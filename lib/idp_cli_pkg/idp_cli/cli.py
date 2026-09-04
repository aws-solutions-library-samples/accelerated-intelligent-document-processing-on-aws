# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
IDP CLI - Main Command Line Interface

Command-line tool for batch document processing with the IDP Accelerator.
"""

import json
import logging
import os
import sys
import time
from typing import TYPE_CHECKING, Dict, Optional

_SETUP_HELP = """\
Error: Required packages not found.

idp-cli requires idp-sdk, idp_common, and their dependencies to be installed.

To fix this, run one of:
  make setup          Install into your current Python environment
  make setup-venv     Create a .venv and install into it

If you already ran 'make setup-venv', activate it first:
  source .venv/bin/activate

If idp_sdk IS installed but fails to provide IDPClient, you likely have the
wrong package: idp-sdk and idp-common are first-party (they live in lib/ and are
not published to PyPI), but those names are squatted on public PyPI. Verify with:
  python scripts/check_first_party_deps.py

See docs/idp-cli.md for details.
"""

_IMPORT_ERROR: Optional[ImportError] = None

# Imports are split into two tiers, and the split is deliberate.
#
# Tier 1 — click / rich / boto3 — are used at MODULE level (the `@click.group()`
# and `@click.option(...)` decorators below, and `console = Console()`). They
# cannot be stubbed to None: the stub itself would blow up during import with
# `AttributeError: 'NoneType' object has no attribute 'group'` long before any
# handler could explain what went wrong. So report and exit here instead.
try:
    import boto3
    import click
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
except ImportError as exc:
    print(_SETUP_HELP, file=sys.stderr)
    print(f"Underlying import error: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

# Tier 2 — IDPClient / display — are only ever used INSIDE functions, so the
# module can finish importing without them. Stub them and keep the original
# exception for main() to report.
#
# Keep that exception: swallowing it makes a partially-broken install look like
# "nothing is installed". In particular, an `idp_sdk` that imports but does not
# export `IDPClient` means the squatted PyPI package is installed instead of the
# first-party one — a different problem needing a different fix — and any later
# use of a None stub would surface as an unrelated AttributeError far from the
# real cause.
try:
    from idp_sdk import IDPClient

    from . import display
except ImportError as exc:
    _IMPORT_ERROR = exc
    if not TYPE_CHECKING:
        IDPClient = None
        display = None

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Console is guaranteed importable — a missing rich exits above, not stubs to None.
console = Console()


def _build_from_local_code(
    from_code_dir: str,
    region: str,
    stack_name: str,
    *,
    headless: bool = False,
    govcloud: bool = False,
    bucket_basename: Optional[str] = None,
    prefix: Optional[str] = None,
    public: bool = False,
    max_workers: Optional[int] = None,
    clean_build: bool = False,
    no_validate: bool = False,
    verbose: bool = False,
    lint: bool = True,
) -> tuple:
    """
    Build project from local code using the SDK publish operation.

    Args:
        from_code_dir: Path to project root directory
        region: AWS region
        stack_name: CloudFormation stack name (unused but kept for signature compatibility)
        headless: If True, also generate a headless template variant.
        bucket: S3 bucket basename for artifacts (auto-generated if not provided).
        prefix: S3 key prefix for artifacts (default: idp-cli).
        public: If True, make artifacts publicly readable.
        max_workers: Max concurrent build workers.
        clean_build: Force full rebuild.
        no_validate: Skip CloudFormation template validation.
        verbose: Enable verbose output.
        lint: Enable linting (default: True).

    Returns:
        Tuple of (template_path, template_url) on success.
        If headless, returns the headless template path/url instead.

    Raises:
        SystemExit: On build failure
    """
    console.print("[bold cyan]Building project from source...[/bold cyan]")
    console.print(f"[dim]Source: {from_code_dir}[/dim]")
    console.print(f"[dim]Region: {region}[/dim]")
    if headless:
        console.print("[dim]Mode: headless[/dim]")
    if bucket_basename:
        console.print(f"[dim]Bucket: {bucket_basename}[/dim]")
    if prefix:
        console.print(f"[dim]Prefix: {prefix}[/dim]")
    console.print()

    try:
        client = IDPClient(region=region)
        result = client.publish.build(
            source_dir=from_code_dir,
            bucket=bucket_basename,
            prefix=prefix,
            region=region,
            headless=headless,
            govcloud=govcloud,
            public=public,
            max_workers=max_workers,
            clean_build=clean_build,
            no_validate=no_validate,
            verbose=verbose,
            lint=lint,
        )

        if not result.success:
            console.print(f"[red]✗ Build failed: {result.error}[/red]")
            sys.exit(1)

        console.print()

        # Return govcloud template if govcloud mode
        if govcloud and result.govcloud_template_path:
            console.print(
                f"[green]✓ Build complete (GovCloud). Template: {result.govcloud_template_path}[/green]"
            )
            return result.govcloud_template_path, result.govcloud_template_url

        # Return headless template if headless mode
        if headless and result.headless_template_path:
            console.print(
                f"[green]✓ Build complete (headless). Template: {result.headless_template_path}[/green]"
            )
            return result.headless_template_path, result.headless_template_url

        console.print(
            f"[green]✓ Build complete. Template: {result.template_path}[/green]"
        )
        console.print()
        return result.template_path, result.template_url

    except Exception as e:
        console.print(f"[red]✗ Error during build: {e}[/red]")
        sys.exit(1)


def _display_deployment_failure(client, stack_name: str, result):
    """
    Display detailed failure analysis when a deployment fails.

    Recursively collects failure events from main and nested stacks
    to identify and display root causes.

    Args:
        client: IDPClient instance
        stack_name: Stack name
        result: StackMonitorResult or StackDeploymentResult
    """
    operation = (
        result.operation
        if hasattr(result, "operation")
        else result.get("operation", "UNKNOWN")
    )
    status = (
        result.status if hasattr(result, "status") else result.get("status", "UNKNOWN")
    )
    error = result.error if hasattr(result, "error") else result.get("error", "Unknown")

    console.print(f"\n[red]✗ Stack {operation} failed![/red]")
    console.print(f"Status: {status}")
    console.print()

    # Get detailed failure analysis
    # Pass deploy_start_time if available to filter stale events from previous deployments
    try:
        deploy_start_time = (
            result.deploy_start_time
            if hasattr(result, "deploy_start_time")
            else result.get("deploy_start_time")
            if isinstance(result, dict)
            else None
        )
        analysis = client.stack.get_failure_analysis(
            stack_name, deploy_start_time=deploy_start_time
        )

        if analysis.root_causes:
            console.print("[bold red]Root Cause Analysis:[/bold red]")
            console.print("━" * 70)
            for i, cause in enumerate(analysis.root_causes, 1):
                # Build the location string
                if cause.stack_path:
                    location = f"{cause.stack_path} → {cause.resource}"
                else:
                    location = cause.resource

                # Add resource type if available
                type_hint = f" ({cause.resource_type})" if cause.resource_type else ""

                console.print(f"  [red]✗[/red] {location}{type_hint}")
                console.print(f"    [yellow]{cause.reason}[/yellow]")
                if i < len(analysis.root_causes):
                    console.print()

            console.print("━" * 70)

            # Show count of cascade/other failures for context
            if analysis.cascade_count > 0:
                console.print(
                    f"[dim]  ({analysis.cascade_count} additional resource(s) cancelled due to the above failure(s))[/dim]"
                )

            console.print()
        else:
            # No root causes found - fall back to simple error
            console.print(f"Error: {error or 'Unknown'}")
            console.print()

    except Exception as e:
        # If analysis fails, fall back to simple error message
        logger.debug(f"Failure analysis error: {e}")
        console.print(f"Error: {error or 'Unknown'}")
        console.print()


# Region-specific template URLs
TEMPLATE_URLS = {
    "us-west-2": "https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main.yaml",
    "us-east-1": "https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main.yaml",
    "eu-central-1": "https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main.yaml",
}


def _parse_tags(tags: Optional[str]) -> Dict[str, str]:
    """Parse a --tags string (key=value,key2=value2) into a dict.

    Unlike --parameters, tag keys may contain spaces and characters like
    . : / + - _, so we split on commas then on the first '=' rather than
    using a key-name regex. Commas are not supported inside tag values.

    Raises click.BadParameter on malformed input (missing '=' or empty key).
    """
    result: Dict[str, str] = {}
    if not tags:
        return result
    for pair in tags.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise click.BadParameter(
                f"Invalid tag '{pair}'. Expected key=value,key2=value2.",
                param_hint="--tags",
            )
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise click.BadParameter(
                f"Invalid tag '{pair}': empty key.",
                param_hint="--tags",
            )
        result[key] = value
    return result


@click.group()
@click.version_option(version="0.6.7")
def cli():
    """
    IDP CLI - Batch document processing for IDP Accelerator

    This tool provides commands for:
    - Stack deployment
    - Batch document upload and processing
    - Progress monitoring with live updates
    - Status checking and reporting

    Global Options:
      --profile PROFILE    AWS profile name to use for credentials.
                          Can be placed anywhere in the command.
                          Example: idp-cli --profile my-profile run-inference ...
                          Example: idp-cli run-inference --profile my-profile ...
    """
    pass


@cli.command()
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--admin-email", help="Admin user email address (required for new stacks)"
)
@click.option(
    "--from-code",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Deploy from local code by building with publish.py (path to project root)",
)
@click.option(
    "--template-url",
    help="URL to CloudFormation template in S3 (default: auto-selected based on region)",
)
@click.option(
    "--template-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a local CloudFormation template file (e.g., .aws-sam/idp-main.yaml from a previous publish)",
)
@click.option(
    "--max-concurrent",
    default=100,
    type=int,
    help="Maximum concurrent workflows (default: 100)",
)
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARN", "ERROR"]),
    help="Logging level (default: INFO)",
)
@click.option(
    "--enable-hitl",
    default="false",
    type=click.Choice(["true", "false"]),
    help="Enable Human-in-the-Loop (default: false)",
)
@click.option(
    "--custom-config",
    help="Path to local config file or S3 URI (e.g., ./config.yaml or s3://bucket/config.yaml)",
)
@click.option("--parameters", help="Additional parameters as key=value,key2=value2")
@click.option(
    "--tags",
    help=(
        "Stack tags as key=value,key2=value2. Propagated by CloudFormation "
        "to all taggable resources and nested stacks. On update, providing "
        "tags replaces the stack's tag set; omitting them preserves existing tags."
    ),
)
@click.option("--wait", is_flag=True, help="Wait for stack operation to complete")
@click.option(
    "--no-rollback", is_flag=True, help="Disable rollback on stack creation failure"
)
@click.option("--region", help="AWS region (optional)")
@click.option("--role-arn", help="CloudFormation service role ARN")
@click.option(
    "--headless",
    is_flag=True,
    help="Deploy headless (no UI/AppSync/Cognito/WAF) — for API-only or GovCloud deployments",
)
@click.option(
    "--govcloud",
    is_flag=True,
    help=(
        "Deploy the GovCloud template variant: removes all AWS::CloudFront::* "
        "resources (unavailable in GovCloud) and forces API Gateway Web UI "
        "hosting, keeping the full UI. Mutually exclusive with --headless."
    ),
)
@click.option(
    "--bucket-basename",
    default=None,
    help="S3 bucket basename for artifacts — region is appended automatically (auto-generated if not provided, used with --from-code)",
)
@click.option(
    "--prefix",
    default=None,
    help="S3 key prefix for artifacts (default: idp-cli, used with --from-code)",
)
@click.option(
    "--public",
    is_flag=True,
    help="Make S3 artifacts publicly readable (used with --from-code)",
)
@click.option(
    "--build-max-workers",
    type=int,
    default=None,
    help="Concurrent build workers (used with --from-code)",
)
@click.option(
    "--clean-build",
    is_flag=True,
    help="Force full rebuild by deleting checksums (used with --from-code)",
)
@click.option(
    "--no-validate-template",
    is_flag=True,
    help="Skip CloudFormation template validation (used with --from-code)",
)
def deploy(
    stack_name: str,
    admin_email: Optional[str],
    from_code: Optional[str],
    template_url: str,
    template_file: Optional[str],
    max_concurrent: int,
    log_level: str,
    enable_hitl: str,
    custom_config: Optional[str],
    parameters: Optional[str],
    tags: Optional[str],
    wait: bool,
    no_rollback: bool,
    region: Optional[str],
    role_arn: Optional[str],
    headless: bool,
    govcloud: bool,
    bucket_basename: Optional[str],
    prefix: Optional[str],
    public: bool,
    build_max_workers: Optional[int],
    clean_build: bool,
    no_validate_template: bool,
):
    """
    Deploy or update IDP stack from command line
    
    For new stacks, --admin-email is required.
    For existing stacks, only specify parameters you want to update.
    
    Headless mode (--headless) deploys without UI, AppSync, Cognito, WAF,
    Agents, HITL, and Knowledge Base — suitable for API-only or GovCloud use.
    
    To build templates without deploying, use 'idp-cli publish' instead.
    
    Examples:
    
      # Create new stack
      idp-cli deploy --stack-name my-idp --admin-email user@example.com
      
      # Deploy from local code
      idp-cli deploy --stack-name my-idp --from-code . --admin-email user@example.com --wait
      
      # Deploy headless from local code
      idp-cli deploy --stack-name my-idp --from-code . --headless --wait
      
      # Deploy headless from pre-built template
      idp-cli deploy --stack-name my-idp --headless --wait
      
      # Deploy from code with custom bucket/prefix
      idp-cli deploy --stack-name my-idp --from-code . --bucket my-artifacts --prefix v1 --wait
      
      # Create with additional parameters
      idp-cli deploy --stack-name my-idp \\
          --admin-email user@example.com \\
          --parameters "DataRetentionInDays=90,ErrorThreshold=5"
    """
    try:
        # Validate mutually exclusive options
        exclusive_count = sum(1 for x in [from_code, template_url, template_file] if x)
        if exclusive_count > 1:
            console.print(
                "[red]✗ Error: Cannot specify more than one of --from-code, --template-url, --template-file[/red]"
            )
            sys.exit(1)

        if headless and govcloud:
            console.print(
                "[red]✗ Error: --headless and --govcloud are mutually exclusive. "
                "--headless removes the UI entirely; --govcloud keeps the UI but "
                "removes CloudFront and uses API Gateway hosting.[/red]"
            )
            sys.exit(1)

        # Auto-detect region if not provided
        if not region:
            import boto3

            session = boto3.session.Session()  # type: ignore
            region = session.region_name
            if not region:
                raise ValueError(
                    "Region could not be determined. Please specify --region or configure AWS_DEFAULT_REGION"
                )

        # Handle deployment from local code
        template_path = None
        if from_code:
            template_path, template_url = _build_from_local_code(
                from_code,
                region,
                stack_name,
                headless=headless,
                govcloud=govcloud,
                bucket_basename=bucket_basename,
                prefix=prefix,
                public=public,
                max_workers=build_max_workers,
                clean_build=clean_build,
                no_validate=no_validate_template,
            )

        # Handle local template file (from a previous publish)
        elif template_file:
            template_path = os.path.abspath(template_file)
            console.print(f"[bold]Using local template: {template_path}[/bold]")

        # Handle govcloud mode for pre-built templates (no --from-code).
        # Download the default template, strip CloudFront + force API Gateway
        # hosting, upload the transformed template, and deploy that.
        elif govcloud and not template_url:
            console.print("[bold cyan]Generating GovCloud template...[/bold cyan]")
            if region in TEMPLATE_URLS:
                source_url = TEMPLATE_URLS[region]
            else:
                supported_regions = ", ".join(TEMPLATE_URLS.keys())
                raise ValueError(
                    f"Region '{region}' is not supported for --govcloud without "
                    f"--from-code/--template-url. Supported regions: {supported_regions}. "
                    f"Use --from-code or --template-url explicitly."
                )

            import tempfile

            import boto3 as _boto3
            import requests

            from idp_sdk.operations.publish import DEFAULT_GOVCLOUD_LINT_REGION

            with tempfile.TemporaryDirectory() as tmpdir:
                local_template = os.path.join(tmpdir, "idp-main.yaml")
                console.print(f"[dim]Downloading template from {source_url}...[/dim]")
                resp = requests.get(source_url, timeout=60)
                resp.raise_for_status()
                with open(local_template, "wb") as f:
                    f.write(resp.content)

                govcloud_template = os.path.join(tmpdir, "idp-govcloud.yaml")
                client_tmp = IDPClient(region=region)
                # Lint against the actual deploy region when it is GovCloud;
                # otherwise fall back to the default GovCloud lint region.
                gc_lint_region = (
                    region
                    if region and region.startswith("us-gov-")
                    else DEFAULT_GOVCLOUD_LINT_REGION
                )
                transform_result = client_tmp.publish.transform_template_govcloud(
                    source_template=local_template,
                    output_path=govcloud_template,
                    lint_region=gc_lint_region,
                )
                if not transform_result.success:
                    console.print(
                        f"[red]✗ GovCloud transformation failed: {transform_result.error}[/red]"
                    )
                    sys.exit(1)

                sts = _boto3.client("sts", region_name=region)
                account_id = sts.get_caller_identity()["Account"]
                bucket_name = f"idp-accelerator-artifacts-{account_id}-{region}"
                s3 = _boto3.client("s3", region_name=region)
                s3_key = "idp-cli/idp-govcloud.yaml"
                s3.upload_file(
                    govcloud_template,
                    bucket_name,
                    s3_key,
                    ExtraArgs={"ContentType": "text/yaml"},
                )
                template_url = (
                    f"https://s3.{region}.amazonaws.com/{bucket_name}/{s3_key}"
                )
                console.print(
                    f"[green]✓ GovCloud template uploaded: {template_url}[/green]"
                )

        # Handle headless mode for pre-built templates (no --from-code)
        elif headless and not template_url:
            # Download default template, transform to headless, upload to temp bucket
            console.print("[bold cyan]Generating headless template...[/bold cyan]")
            if region in TEMPLATE_URLS:
                source_url = TEMPLATE_URLS[region]
            else:
                supported_regions = ", ".join(TEMPLATE_URLS.keys())
                raise ValueError(
                    f"Region '{region}' is not supported for headless mode. "
                    f"Supported regions: {supported_regions}. "
                    f"Please use --from-code or --template-url explicitly."
                )

            # Download template, transform, upload
            import tempfile

            import boto3 as _boto3
            import requests

            with tempfile.TemporaryDirectory() as tmpdir:
                # Download
                local_template = os.path.join(tmpdir, "idp-main.yaml")
                console.print(f"[dim]Downloading template from {source_url}...[/dim]")
                resp = requests.get(source_url, timeout=60)
                resp.raise_for_status()
                with open(local_template, "wb") as f:
                    f.write(resp.content)

                # Transform
                headless_template = os.path.join(tmpdir, "idp-headless.yaml")
                client_tmp = IDPClient(region=region)
                is_govcloud = region and region.startswith("us-gov-")
                transform_result = client_tmp.publish.transform_template_headless(
                    source_template=local_template,
                    output_path=headless_template,
                    update_govcloud_config=is_govcloud,
                )
                if not transform_result.success:
                    console.print(
                        f"[red]✗ Headless transformation failed: {transform_result.error}[/red]"
                    )
                    sys.exit(1)

                # Upload to per-account bucket
                sts = _boto3.client("sts", region_name=region)
                account_id = sts.get_caller_identity()["Account"]
                bucket_name = f"idp-accelerator-artifacts-{account_id}-{region}"
                s3 = _boto3.client("s3", region_name=region)
                s3_key = "idp-cli/idp-headless.yaml"
                s3.upload_file(
                    headless_template,
                    bucket_name,
                    s3_key,
                    ExtraArgs={"ContentType": "text/yaml"},
                )
                template_url = (
                    f"https://s3.{region}.amazonaws.com/{bucket_name}/{s3_key}"
                )
                # Note: template_path left as None — the temp file will be deleted
                # when this `with` block exits. Deploy will use template_url instead.
                console.print(
                    f"[green]✓ Headless template uploaded: {template_url}[/green]"
                )

        # Determine template URL (user-provided takes precedence)
        elif not template_url:
            if region in TEMPLATE_URLS:
                template_url = TEMPLATE_URLS[region]
                console.print(f"[bold]Using template for region: {region}[/bold]")
            else:
                supported_regions = ", ".join(TEMPLATE_URLS.keys())
                raise ValueError(
                    f"Region '{region}' is not supported. "
                    f"Supported regions: {supported_regions}. "
                    f"Please provide --template-url explicitly for other regions."
                )

        # Initialize SDK client
        client = IDPClient(stack_name=stack_name, region=region)

        # Check if stack has an operation in progress
        in_progress = client.stack.check_in_progress()
        if in_progress:
            # Stack has an operation in progress - switch to monitoring mode
            operation = in_progress.operation
            status = in_progress.status

            console.print(
                f"[bold yellow]Stack '{stack_name}' has an operation in progress[/bold yellow]"
            )
            console.print(f"Current status: [cyan]{status}[/cyan]")
            console.print()
            console.print("[bold]Switching to monitoring mode...[/bold]")
            console.print()

            # Monitor the existing operation
            with console.status(f"[bold cyan]Monitoring {operation}...[/bold cyan]"):
                result = client.stack.monitor(operation=operation)

            # Show results
            if result.success:
                console.print(
                    f"\n[green]✓ Stack {result.operation} completed successfully![/green]\n"
                )

                # Show outputs for non-delete operations
                if operation != "DELETE":
                    outputs = result.outputs
                    if outputs:
                        console.print("[bold]Important Outputs:[/bold]")
                        console.print(
                            f"  Application URL: [cyan]{outputs.get('ApplicationWebURL', 'N/A')}[/cyan]"
                        )
                        console.print(
                            f"  Input Bucket: {outputs.get('S3InputBucketName', 'N/A')}"
                        )
                        console.print(
                            f"  Output Bucket: {outputs.get('S3OutputBucketName', 'N/A')}"
                        )
                        console.print()

                console.print("[bold]Next Steps:[/bold]")
                console.print("1. Check your email for temporary admin password")
                console.print("2. Enable Bedrock model access (see README)")
                console.print("3. Process documents:")
                console.print(
                    f"   [cyan]idp-cli run-inference --stack-name {stack_name} --manifest docs.csv[/cyan]"
                )
                console.print()
            else:
                _display_deployment_failure(client, stack_name, result)
                sys.exit(1)

            return  # Exit after monitoring

        # Check if stack exists
        stack_exists = client.stack.exists()

        if stack_exists:
            # Stack exists - updating (all parameters are optional)
            console.print(
                f"[bold blue]Updating existing IDP stack: {stack_name}[/bold blue]"
            )
            if admin_email:
                console.print(f"Admin Email: {admin_email}")
        else:
            # New stack - require admin_email unless --headless (headless template
            # strips Cognito and has no AdminEmail parameter, so the value is
            # unused and would cause a CFN ValidationError if passed through).
            console.print(
                f"[bold blue]Creating new IDP stack: {stack_name}[/bold blue]"
            )

            if headless:
                # Drop any user-supplied admin_email so it doesn't reach CFN.
                if admin_email:
                    console.print(
                        "[yellow]--admin-email is ignored with --headless "
                        "(no Cognito in headless template)[/yellow]"
                    )
                admin_email = None
            else:
                if not admin_email:
                    console.print(
                        "[red]✗ Error: --admin-email is required when creating a new stack[/red]"
                    )
                    sys.exit(1)

                console.print(f"Admin Email: {admin_email}")

        console.print()

        # Parse additional parameters
        additional_params = {}
        if parameters:
            # Parse key=value pairs separated by commas, but handle values
            # that themselves contain commas (e.g., subnet lists).
            # Strategy: split on commas that are followed by a key= pattern.
            import re

            for match in re.finditer(
                r"([A-Za-z][A-Za-z0-9]*)=((?:(?![A-Za-z][A-Za-z0-9]*=).)*)",
                parameters,
            ):
                key = match.group(1).strip()
                value = match.group(2).strip().rstrip(",")
                additional_params[key] = value

        # Parse stack tags (key=value,key2=value2), propagated by CloudFormation
        # to all taggable resources and nested stacks.
        tags_dict = _parse_tags(tags)

        # Note: --headless (the CLI flag) controls TEMPLATE TRANSFORMATION
        # — strip UI / AppSync / Cognito / WAF / Agents / HITL / KB from the
        # template. The CFN parameter `EnableJobsApi=true` is a separate,
        # additive opt-in for the Private API Gateway (/jobs REST API), which
        # by design requires a VPC + ApiGatewayVpcEndpointId. Users who want
        # the Jobs API must pass `--parameters EnableJobsApi=true,...VPC params`
        # explicitly.

        # Deploy stack via SDK (build_parameters is called internally by client.stack.deploy)
        # Debug: show custom config path hint before deploy
        if custom_config:
            console.print(f"[yellow]DEBUG: CustomConfig = {custom_config}[/yellow]")

        # Deploy stack
        with console.status("[bold green]Deploying stack..."):
            result = client.stack.deploy(
                template_url=template_url,
                template_path=template_path,
                admin_email=admin_email,
                max_concurrent=max_concurrent if max_concurrent != 100 else None,
                log_level=log_level if log_level != "INFO" else None,
                enable_hitl=enable_hitl == "true" if enable_hitl != "false" else None,
                custom_config=custom_config,
                parameters=additional_params,
                tags=tags_dict or None,
                wait=wait,
                no_rollback=no_rollback,
                role_arn=role_arn,
            )

        # Show results
        # Success if operation completed successfully OR was successfully initiated
        is_success = result.success or result.status == "INITIATED"

        if is_success:
            if result.success:
                # Completed (with --wait)
                console.print(
                    f"\n[green]✓ Stack {result.operation} completed successfully![/green]\n"
                )

                # Show outputs
                outputs = result.outputs
                if outputs:
                    console.print("[bold]Important Outputs:[/bold]")
                    console.print(
                        f"  Application URL: [cyan]{outputs.get('ApplicationWebURL', 'N/A')}[/cyan]"
                    )
                    console.print(
                        f"  Input Bucket: {outputs.get('S3InputBucketName', 'N/A')}"
                    )
                    console.print(
                        f"  Output Bucket: {outputs.get('S3OutputBucketName', 'N/A')}"
                    )
                    console.print()

                console.print("[bold]Next Steps:[/bold]")
                console.print("1. Check your email for temporary admin password")
                console.print("2. Enable Bedrock model access (see README)")
                console.print("3. Process documents:")
                console.print(
                    f"   [cyan]idp-cli run-inference --stack-name {stack_name} --manifest docs.csv[/cyan]"
                )
                console.print()
            else:
                # Initiated (without --wait)
                console.print(
                    f"\n[green]✓ Stack {result.operation} initiated successfully![/green]\n"
                )
                console.print("[bold]Monitor progress:[/bold]")
                console.print(f"  AWS Console: CloudFormation → Stacks → {stack_name}")
                console.print()
                console.print("[bold]Or use --wait flag to monitor in CLI:[/bold]")
                console.print(
                    f"  [cyan]idp-cli deploy --stack-name {stack_name} --wait[/cyan]"
                )
                console.print()
        else:
            _display_deployment_failure(client, stack_name, result)
            sys.exit(1)

    except FileNotFoundError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error deploying stack: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--empty-buckets",
    is_flag=True,
    help="Empty S3 buckets before deletion (required if buckets contain data)",
)
@click.option(
    "--force-delete-all",
    is_flag=True,
    help="Force delete ALL remaining resources after CloudFormation deletion (S3 buckets, CloudWatch logs, DynamoDB tables). This cannot be undone.",
)
@click.option(
    "--wait",
    is_flag=True,
    help="Wait for deletion to complete (same as deploy)",
)
@click.option("--region", help="AWS region (optional)")
def delete(
    stack_name: str,
    force: bool,
    empty_buckets: bool,
    force_delete_all: bool,
    wait: bool,
    region: Optional[str],
):
    """
    Delete an IDP CloudFormation stack

    ⚠️  WARNING: This permanently deletes all stack resources.

    S3 buckets configured with RetainExceptOnCreate will be deleted if empty.
    Use --empty-buckets to automatically empty buckets before deletion.
    Use --force-delete-all to delete ALL remaining resources after CloudFormation deletion.

    Examples:

      # Interactive deletion with confirmation
      idp-cli delete --stack-name test-stack

      # Automated deletion (skip confirmation)
      idp-cli delete --stack-name test-stack --force

      # Delete with automatic bucket emptying
      idp-cli delete --stack-name test-stack --empty-buckets --force

      # Force delete ALL remaining resources (S3, logs, DynamoDB)
      idp-cli delete --stack-name test-stack --force-delete-all --force

      # Wait for deletion to complete
      idp-cli delete --stack-name test-stack --force --wait
    """
    try:
        client = IDPClient(stack_name=stack_name, region=region)

        # Check if stack has an operation in progress
        in_progress = client.stack.check_in_progress()
        if in_progress:
            operation = in_progress.operation
            status = in_progress.status

            if operation == "DELETE":
                # Delete already in progress - monitor it
                console.print(
                    f"[bold yellow]Stack '{stack_name}' is already being deleted[/bold yellow]"
                )
                console.print(f"Current status: [cyan]{status}[/cyan]")
                console.print()
                console.print("[bold]Switching to monitoring mode...[/bold]")
                console.print()

                # Monitor the deletion
                with console.status("[bold cyan]Monitoring DELETE...[/bold cyan]"):
                    result = client.stack.monitor(operation="DELETE")

                if result.success:
                    console.print("\n[green]✓ Stack deleted successfully![/green]")
                    console.print(f"Stack: {stack_name}")
                    console.print(f"Status: {result.status}")
                else:
                    console.print("\n[red]✗ Stack deletion failed![/red]")
                    console.print(f"Status: {result.status}")
                    console.print(f"Error: {result.error or 'Unknown'}")
                    sys.exit(1)

                return  # Exit after monitoring
            else:
                # Non-delete operation in progress (CREATE/UPDATE) - offer to cancel and delete
                console.print(
                    f"[bold yellow]Stack '{stack_name}' has an operation in progress: {status}[/bold yellow]"
                )
                console.print()

                if not force:
                    console.print("[bold]Options:[/bold]")
                    console.print(
                        f"  \\[Y] Cancel the {operation} and proceed with deletion (default)"
                    )
                    console.print(
                        f"  \\[w] Wait for {operation} to complete first, then delete"
                    )
                    console.print("  \\[n] Abort - do not delete")
                    console.print()
                    console.print("Choose \\[Y/w/n]: ", end="")
                    response = input().strip().lower()
                    if not response:
                        response = "y"

                    if response in ["n", "no"]:
                        console.print("[yellow]Deletion cancelled[/yellow]")
                        sys.exit(0)
                    elif response in ["w", "wait"]:
                        console.print()
                        console.print(
                            f"[bold]Waiting for {operation} to complete...[/bold]"
                        )
                        console.print()

                        # Monitor the current operation
                        with console.status(
                            f"[bold cyan]Monitoring {operation}...[/bold cyan]"
                        ):
                            monitor_result = client.stack.monitor(operation=operation)

                        if not monitor_result.success:
                            console.print(f"\n[red]✗ {operation} failed![/red]")
                            console.print(f"Status: {monitor_result.status}")
                            # Continue to deletion - user may still want to delete failed stack
                        else:
                            console.print(f"\n[green]✓ {operation} completed![/green]")

                        # Now proceed with deletion (fall through to normal deletion flow)
                        console.print()
                        console.print("[bold]Proceeding with stack deletion...[/bold]")
                        console.print()
                    else:  # yes - cancel and delete
                        console.print()
                        # CloudFormation allows deleting a stack even during CREATE_IN_PROGRESS
                        # It will stop creating resources and start deleting what was created
                        console.print(
                            f"[bold yellow]Deleting stack (will cancel {operation} in progress)...[/bold yellow]"
                        )
                else:
                    # Force mode - automatically cancel and delete
                    console.print(
                        "[bold yellow]Force mode: Canceling operation and proceeding with deletion...[/bold yellow]"
                    )
                    console.print()

                    if operation == "UPDATE":
                        cancel_result = client.stack.cancel_update()
                        if not cancel_result.success:
                            console.print(
                                f"[yellow]Warning: Could not cancel update: {cancel_result.error}[/yellow]"
                            )

                    # Wait for stable state
                    with console.status(
                        "[bold cyan]Waiting for stable state...[/bold cyan]"
                    ):
                        stable_result = client.stack.wait_for_stable_state(
                            timeout_seconds=1200
                        )

                    if not stable_result.success:
                        console.print(
                            f"[red]✗ Timeout waiting for stable state: {stable_result.message}[/red]"
                        )
                        sys.exit(1)

                    console.print(
                        f"[green]✓ Stack reached stable state: {stable_result.status}[/green]"
                    )
                    console.print()

        # Check if stack exists
        if not client.stack.exists():
            console.print(f"[red]✗ Stack '{stack_name}' does not exist[/red]")
            sys.exit(1)

        # Get bucket information
        console.print(f"[bold blue]Analyzing stack: {stack_name}[/bold blue]")
        bucket_info = client.stack.get_bucket_info()

        # Show warning with bucket details
        console.print()
        if force_delete_all:
            console.print("[bold red]⚠️  WARNING: FORCE DELETE ALL RESOURCES[/bold red]")
        else:
            console.print("[bold red]⚠️  WARNING: Stack Deletion[/bold red]")
        console.print("━" * 60)
        console.print(f"Stack: [cyan]{stack_name}[/cyan]")
        console.print(f"Region: {region or 'default'}")

        if bucket_info:
            console.print()
            console.print("[bold]S3 Buckets:[/bold]")
            has_data = False
            for bucket in bucket_info:
                obj_count = bucket.object_count
                size = bucket.size_display
                logical_id = bucket.logical_id

                if obj_count > 0:
                    has_data = True
                    console.print(
                        f"  • {logical_id}: [yellow]{obj_count} objects ({size})[/yellow]"
                    )
                else:
                    console.print(f"  • {logical_id}: [green]empty[/green]")

            if has_data and not empty_buckets and not force_delete_all:
                console.print()
                console.print("[bold red]⚠️  Buckets contain data![/bold red]")
                console.print("Deletion will FAIL unless you:")
                console.print("  1. Use --empty-buckets flag to auto-delete data, OR")
                console.print("  2. Use --force-delete-all to delete everything, OR")
                console.print("  3. Manually empty buckets first")

        if force_delete_all:
            console.print()
            console.print("[bold red]⚠️  FORCE DELETE ALL will remove:[/bold red]")
            console.print("  • All S3 buckets (including LoggingBucket)")
            console.print("  • All CloudWatch Log Groups")
            console.print("  • All DynamoDB Tables")
            console.print("  • Any other retained resources")
            console.print()
            console.print(
                "[bold yellow]This happens AFTER CloudFormation deletion completes[/bold yellow]"
            )

        console.print()
        console.print("[bold red]This action cannot be undone.[/bold red]")
        console.print("━" * 60)
        console.print()

        # Confirmation unless --force
        if not force:
            if force_delete_all:
                response = click.confirm(
                    "Are you ABSOLUTELY sure you want to force delete ALL resources?",
                    default=False,
                )
            else:
                response = click.confirm(
                    "Are you sure you want to delete this stack?", default=False
                )

            if not response:
                console.print("[yellow]Deletion cancelled[/yellow]")
                return

            # Double confirmation if --empty-buckets (and not force-delete-all)
            if empty_buckets and not force_delete_all:
                console.print()
                console.print(
                    "[bold red]⚠️  You are about to permanently delete all bucket data![/bold red]"
                )
                response = click.confirm(
                    "Are you ABSOLUTELY sure you want to empty buckets and delete the stack?",
                    default=False,
                )
                if not response:
                    console.print("[yellow]Deletion cancelled[/yellow]")
                    return

        # Perform deletion
        console.print()
        with console.status("[bold red]Deleting stack..."):
            result = client.stack.delete(
                empty_buckets=empty_buckets,
                force_delete_all=force_delete_all,
                wait=wait,
            )

        # Show CloudFormation deletion results.
        # success=True  → deletion completed (waited to DELETE_COMPLETE)
        # success=False + status=INITIATED → deletion started but not waited on
        # success=False + other status    → genuine failure
        initiated_only = not result.success and result.status == "INITIATED"

        if result.success:
            console.print("\n[green]✓ Stack deleted successfully![/green]")
            console.print(f"Stack: {stack_name}")
            console.print(f"Status: {result.status}")
        elif initiated_only:
            console.print("\n[green]✓ Stack deletion initiated![/green]")
            console.print(f"Stack: {stack_name}")
            console.print(f"Region: {region or 'default'}")
            console.print()
            console.print("[bold]Monitor progress in the AWS Console:[/bold]")
            console.print(f"  CloudFormation → Stacks → {stack_name}")
            console.print()
            console.print("[bold]Or wait for it with:[/bold]")
            console.print(
                f"  [cyan]idp-cli delete --stack-name {stack_name} --force --wait[/cyan]"
            )
        else:
            console.print("\n[red]✗ Stack deletion failed![/red]")
            console.print(f"Status: {result.status}")
            console.print(f"Error: {result.error or 'Unknown'}")

            if result.error and "bucket" in result.error.lower():
                console.print()
                console.print(
                    "[yellow]Tip: Try again with --empty-buckets or --force-delete-all flag[/yellow]"
                )

            if not force_delete_all:
                sys.exit(1)
            else:
                console.print()
                console.print(
                    "[yellow]Stack deletion failed, but continuing with force cleanup...[/yellow]"
                )

        # Post-deletion cleanup results from --force-delete-all.
        # The SDK already ran cleanup_retained_resources(); we just display the results.
        if force_delete_all:
            console.print()
            console.print("[bold blue]━" * 60 + "[/bold blue]")
            console.print(
                "[bold blue]Starting force cleanup of retained resources...[/bold blue]"
            )
            console.print("[bold blue]━" * 60 + "[/bold blue]")

            try:
                cleanup_result = result.cleanup_result or {}

                # Show cleanup summary
                console.print()
                console.print("[bold green]✓ Cleanup phase complete![/bold green]")
                console.print()

                total_deleted = (
                    len(cleanup_result.get("dynamodb_deleted", []))
                    + len(cleanup_result.get("logs_deleted", []))
                    + len(cleanup_result.get("buckets_deleted", []))
                )

                if total_deleted > 0:
                    console.print("[bold]Resources deleted:[/bold]")

                    if cleanup_result.get("dynamodb_deleted"):
                        console.print(
                            f"  • DynamoDB Tables: {len(cleanup_result['dynamodb_deleted'])}"
                        )
                        for table in cleanup_result["dynamodb_deleted"]:
                            console.print(f"    - {table}")

                    if cleanup_result.get("logs_deleted"):
                        console.print(
                            f"  • CloudWatch Log Groups: {len(cleanup_result['logs_deleted'])}"
                        )
                        for log_group in cleanup_result["logs_deleted"]:
                            console.print(f"    - {log_group}")

                    if cleanup_result.get("buckets_deleted"):
                        console.print(
                            f"  • S3 Buckets: {len(cleanup_result['buckets_deleted'])}"
                        )
                        for bucket in cleanup_result["buckets_deleted"]:
                            console.print(f"    - {bucket}")

                if cleanup_result.get("errors"):
                    console.print()
                    console.print(
                        "[bold yellow]⚠️  Some resources could not be deleted:[/bold yellow]"
                    )
                    for error in cleanup_result["errors"]:
                        console.print(f"  • {error['type']}: {error['resource']}")
                        console.print(f"    Error: {error['error']}")

                console.print()

            except Exception as e:
                logger.error(f"Error during cleanup: {e}", exc_info=True)
                console.print(f"\n[red]✗ Cleanup phase error: {e}[/red]")
                console.print(
                    "[yellow]Some resources may remain - check AWS Console[/yellow]"
                )
        elif result.success:
            # Standard deletion without force-delete-all — stack was fully deleted
            console.print()
            console.print(
                "[bold]Note:[/bold] LoggingBucket (if exists) is retained by design."
            )
            console.print("Delete it manually if no longer needed:")
            console.print("  [cyan]aws s3 rb s3://<logging-bucket-name> --force[/cyan]")
            console.print()

    except Exception as e:
        logger.error(f"Error deleting stack: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="delete-documents")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--document-ids",
    help="Comma-separated list of document IDs (S3 object keys) to delete",
)
@click.option(
    "--batch-id",
    help="Delete all documents in this batch (alternative to --document-ids)",
)
@click.option(
    "--pattern",
    help='Wildcard pattern to match document keys (e.g. "batch-123/*.pdf", "*invoice*")',
)
@click.option(
    "--status-filter",
    type=click.Choice(["FAILED", "COMPLETED", "PROCESSING", "QUEUED"]),
    help="Only delete documents with this status (use with --batch-id or --pattern)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be deleted without actually deleting",
)
@click.option(
    "--force",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.option("--region", help="AWS region (optional)")
def delete_documents_cmd(
    stack_name: str,
    document_ids: Optional[str],
    batch_id: Optional[str],
    pattern: Optional[str],
    status_filter: Optional[str],
    dry_run: bool,
    force: bool,
    region: Optional[str],
):
    """
    Delete documents and all associated data from the IDP system

    Permanently deletes documents including:
    - Source files from input bucket
    - Processed outputs from output bucket
    - DynamoDB tracking records
    - List entries in tracking table

    ⚠️  WARNING: This action cannot be undone.

    Examples:

      # Delete specific documents by ID
      idp-cli delete-documents --stack-name my-stack \\
          --document-ids "batch-123/doc1.pdf,batch-123/doc2.pdf"

      # Delete all documents in a batch
      idp-cli delete-documents --stack-name my-stack --batch-id cli-batch-20250123

      # Delete only failed documents in a batch
      idp-cli delete-documents --stack-name my-stack --batch-id cli-batch-20250123 --status-filter FAILED

      # Delete documents matching a wildcard pattern
      idp-cli delete-documents --stack-name my-stack --pattern "batch-123/*.pdf"

      # Delete all failed invoice documents
      idp-cli delete-documents --stack-name my-stack --pattern "*invoice*" --status-filter FAILED

      # Dry run to see what would be deleted
      idp-cli delete-documents --stack-name my-stack --batch-id cli-batch-20250123 --dry-run

      # Force delete without confirmation
      idp-cli delete-documents --stack-name my-stack --document-ids "batch-123/doc1.pdf" --force
    """
    try:
        import boto3

        from idp_common.delete_documents import (
            delete_documents,
            get_documents_by_batch,
            get_documents_by_pattern,
        )
        from idp_sdk import IDPClient

        # Validate input - exactly one of document_ids, batch_id, or pattern required
        selector_count = sum(1 for x in [document_ids, batch_id, pattern] if x)
        if selector_count == 0:
            console.print(
                "[red]✗ Error: Must specify one of --document-ids, --batch-id, or --pattern[/red]"
            )
            sys.exit(1)

        if selector_count > 1:
            console.print(
                "[red]✗ Error: Cannot specify more than one of --document-ids, --batch-id, --pattern[/red]"
            )
            sys.exit(1)

        # Get stack resources
        console.print(f"[bold blue]Connecting to stack: {stack_name}[/bold blue]")
        client = IDPClient(stack_name=stack_name, region=region)
        resources = client.stack.get_resources()

        input_bucket = resources.input_bucket
        output_bucket = resources.output_bucket
        tracking_table_name = resources.documents_table

        if not all([input_bucket, output_bucket, tracking_table_name]):
            console.print("[red]✗ Error: Could not find required stack resources[/red]")
            console.print(f"  InputBucket: {input_bucket}")
            console.print(f"  OutputBucket: {output_bucket}")
            console.print(f"  DocumentsTable: {tracking_table_name}")
            sys.exit(1)

        # Initialize AWS clients
        dynamodb = boto3.resource("dynamodb", region_name=region)
        s3_client = boto3.client("s3", region_name=region)
        tracking_table = dynamodb.Table(tracking_table_name)

        # Get document list
        if document_ids:
            doc_list = [d.strip() for d in document_ids.split(",")]
            console.print(f"Selected {len(doc_list)} document(s) for deletion")
        elif pattern:
            console.print(
                f"[bold blue]Finding documents matching pattern: {pattern}[/bold blue]"
            )
            doc_list = get_documents_by_pattern(
                tracking_table=tracking_table,
                pattern=pattern,
                status_filter=status_filter,
            )
            if not doc_list:
                console.print(
                    f"[yellow]No documents found matching pattern: {pattern}[/yellow]"
                )
                if status_filter:
                    console.print(
                        f"[yellow]  (with status filter: {status_filter})[/yellow]"
                    )
                sys.exit(0)
            console.print(f"Found {len(doc_list)} document(s) matching pattern")
            if status_filter:
                console.print(f"  (filtered by status: {status_filter})")
        else:
            console.print(
                f"[bold blue]Getting documents for batch: {batch_id}[/bold blue]"
            )
            doc_list = get_documents_by_batch(
                tracking_table=tracking_table,
                batch_id=batch_id,
                status_filter=status_filter,
            )
            if not doc_list:
                console.print(
                    f"[yellow]No documents found for batch: {batch_id}[/yellow]"
                )
                if status_filter:
                    console.print(
                        f"[yellow]  (with status filter: {status_filter})[/yellow]"
                    )
                sys.exit(0)
            console.print(f"Found {len(doc_list)} document(s) in batch")
            if status_filter:
                console.print(f"  (filtered by status: {status_filter})")

        # Show what will be deleted
        console.print()
        if dry_run:
            console.print(
                "[bold yellow]DRY RUN - No changes will be made[/bold yellow]"
            )
        console.print("[bold red]⚠️  Documents to be deleted:[/bold red]")
        console.print("━" * 60)
        for doc in doc_list[:10]:  # Show first 10
            console.print(f"  • {doc}")
        if len(doc_list) > 10:
            console.print(f"  ... and {len(doc_list) - 10} more")
        console.print("━" * 60)
        console.print()

        # Confirm unless --force or --dry-run
        if not force and not dry_run:
            response = click.confirm(
                f"Delete {len(doc_list)} document(s) permanently?",
                default=False,
            )
            if not response:
                console.print("[yellow]Deletion cancelled[/yellow]")
                return

        # Perform deletion
        console.print()
        with console.status(f"[bold red]Deleting {len(doc_list)} document(s)..."):
            result = delete_documents(
                object_keys=doc_list,
                tracking_table=tracking_table,
                s3_client=s3_client,
                input_bucket=input_bucket,
                output_bucket=output_bucket,
                dry_run=dry_run,
                continue_on_error=True,
            )

        # Show results
        console.print()
        if dry_run:
            console.print("[bold yellow]DRY RUN COMPLETE[/bold yellow]")
            console.print(f"Would delete {result['total_count']} document(s)")
        elif result["success"]:
            console.print(
                f"[green]✓ Successfully deleted {result['deleted_count']} document(s)[/green]"
            )
        else:
            console.print(
                f"[yellow]⚠ Deleted {result['deleted_count']}/{result['total_count']} document(s)[/yellow]"
            )
            console.print(f"[red]  {result['failed_count']} failed[/red]")

        # Show details for failures
        if result.get("results"):
            failures = [r for r in result["results"] if not r.get("success")]
            if failures and not dry_run:
                console.print()
                console.print("[bold red]Failed deletions:[/bold red]")
                for f in failures[:5]:
                    console.print(f"  • {f['object_key']}")
                    for err in f.get("errors", []):
                        console.print(f"    [red]{err}[/red]")
                if len(failures) > 5:
                    console.print(f"  ... and {len(failures) - 5} more failures")

        console.print()

    except Exception as e:
        logger.error(f"Error deleting documents: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


def _process_impl(
    stack_name: str,
    manifest: Optional[str],
    directory: Optional[str],
    s3_uri: Optional[str],
    test_set: Optional[str],
    context: Optional[str],
    batch_id: Optional[str],
    file_pattern: str,
    recursive: bool,
    config: Optional[str],
    batch_prefix: str,
    monitor: bool,
    refresh_interval: int,
    region: Optional[str],
    number_of_files: Optional[int],
    config_version: Optional[str],
    config_revision: Optional[int] = None,
):
    """Implementation for process and run_inference commands"""
    try:
        # Validate mutually exclusive options
        if not manifest and not directory and not s3_uri and not test_set:
            console.print(
                "[red]✗ Error: Must specify one of: --manifest, --dir, --s3-uri, or --test-set[/red]"
            )
            sys.exit(1)

        input_count = sum(
            1 for x in [manifest, directory, s3_uri, test_set] if x is not None
        )
        if input_count > 1:
            console.print("[red]✗ Error: Cannot specify multiple input sources[/red]")
            sys.exit(1)

        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)

        # Handle test set processing
        if test_set:
            batch_result = _process_test_set(
                stack_name=stack_name,
                test_set_name=test_set,
                context=context,
                region=region,
                client=client,
                number_of_files=number_of_files,
                config_version=config_version,
            )
            # test_set path returns legacy dict — extract fields
            result_batch_id = batch_result["batch_id"]
            result_queued = batch_result.get(
                "queued", batch_result.get("documents_queued", 0)
            )
            result_uploaded = batch_result.get("uploaded", 0)
            result_failed = batch_result.get("failed", 0)
        else:
            # Handle manifest/directory/S3 processing via IDPClient
            if manifest:
                result = client.batch.process(
                    manifest=manifest,
                    batch_prefix=batch_prefix,
                    batch_id=batch_id,
                    number_of_files=number_of_files,
                    config_version=config_version,
                    config_revision=config_revision,
                )
            elif directory:
                result = client.batch.process(
                    directory=directory,
                    file_pattern=file_pattern,
                    recursive=recursive,
                    batch_prefix=batch_prefix,
                    batch_id=batch_id,
                    number_of_files=number_of_files,
                    config_version=config_version,
                    config_revision=config_revision,
                )
            elif s3_uri:
                result = client.batch.process(
                    s3_uri=s3_uri,
                    file_pattern=file_pattern,
                    recursive=recursive,
                    batch_prefix=batch_prefix,
                    batch_id=batch_id,
                    number_of_files=number_of_files,
                    config_version=config_version,
                    config_revision=config_revision,
                )
            else:
                raise ValueError("No input source specified")

            result_batch_id = result.batch_id
            result_queued = result.documents_queued
            result_uploaded = result.documents_uploaded
            result_failed = result.documents_failed

        # Show results
        console.print()
        console.print(f"[bold blue]Batch ID: {result_batch_id}[/bold blue]")
        console.print(f"Documents queued: {result_queued}")

        if result_uploaded > 0:
            console.print(f"Files uploaded: {result_uploaded}")
        if result_failed > 0:
            console.print(f"[red]Files failed: {result_failed}[/red]")

        console.print()

        # Monitor if requested
        if monitor and result_queued > 0:
            _monitor_progress(
                client=client,
                batch_id=result_batch_id,
                refresh_interval=refresh_interval,
            )

    except Exception as e:
        logger.error(f"Error processing batch: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--manifest",
    type=click.Path(exists=True),
    help="Path to manifest file (CSV or JSON)",
)
@click.option(
    "--dir",
    "directory",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Local directory containing documents to process",
)
@click.option("--s3-uri", help="S3 URI to process (e.g., s3://bucket/prefix/)")
@click.option("--test-set", help="Test set ID to process from test set bucket")
@click.option(
    "--context", help="Context description for test run (used with --test-set)"
)
@click.option(
    "--batch-id",
    help="Custom batch ID (auto-generated if not provided, ignored with --test-set)",
)
@click.option(
    "--file-pattern",
    default="*.pdf",
    help="File pattern for directory/S3 scanning (default: *.pdf)",
)
@click.option(
    "--recursive/--no-recursive",
    default=True,
    help="Include subdirectories when scanning (default: recursive)",
)
@click.option(
    "--config",
    type=click.Path(exists=True),
    help="Path to configuration YAML file (optional)",
)
@click.option(
    "--batch-prefix",
    default="cli-batch",
    help="Batch ID prefix (used only if --batch-id not provided, default: cli-batch)",
)
@click.option("--monitor", is_flag=True, help="Monitor progress until completion")
@click.option(
    "--refresh-interval",
    default=5,
    type=int,
    help="Seconds between status checks (default: 5)",
)
@click.option("--region", help="AWS region (optional)")
@click.option(
    "--number-of-files",
    type=int,
    help="Limit number of files to process (for testing purposes)",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    help="Configuration profile to use for processing (e.g., v1, v2); --config-version is the former name and still works",
)
@click.option(
    "--config-revision",
    type=int,
    help=(
        "Revision of --config-profile to pin (e.g. 7). Omit to process under the "
        "profile's current configuration; pinning reproduces exactly what that "
        "revision recorded."
    ),
)
def process(
    stack_name: str,
    manifest: Optional[str],
    directory: Optional[str],
    s3_uri: Optional[str],
    test_set: Optional[str],
    context: Optional[str],
    batch_id: Optional[str],
    file_pattern: str,
    recursive: bool,
    config: Optional[str],
    batch_prefix: str,
    monitor: bool,
    refresh_interval: int,
    region: Optional[str],
    number_of_files: Optional[int],
    config_version: Optional[str],
    config_revision: Optional[int],
):
    """
    Process documents

    Specify documents using ONE of:
      --manifest: Explicit manifest file (CSV or JSON)
                 If manifest contains baseline_source column, automatically creates
                 "idp-cli" test set for Test Studio integration and evaluation
      --dir: Local directory (auto-generates manifest)
      --s3-uri: S3 URI (auto-generates manifest, any bucket)
      --test-set: Process existing test set from test set bucket (use test set ID)

    Test Studio Integration:
      - --test-set: Processes existing test sets and tracks results in Test Studio UI
      - --context: Adds descriptive labels to test runs (e.g., "Model v2.1", "Production validation")
      - Manifests with baselines: Automatically creates test sets for accuracy evaluation
      - All processing appears in Test Studio dashboard for analysis and comparison

    Examples:

      # Process from manifest file
      idp-cli process --stack-name my-stack --manifest docs.csv --monitor

      # Process all PDFs in local directory
      idp-cli process --stack-name my-stack --dir ./documents/ --monitor

      # Process with custom batch ID
      idp-cli process --stack-name my-stack --dir ./docs/ --batch-id my-experiment-v1 --monitor

      # Process S3 URI (any bucket)
      idp-cli process --stack-name my-stack --s3-uri s3://data-lake/archive/2024/ --monitor

      # Process with file pattern
      idp-cli process --stack-name my-stack --dir ./docs/ --file-pattern "invoice*.pdf"

      # Process test set (integrates with Test Studio UI - use test set ID)
      idp-cli process --stack-name my-stack --test-set fcc-example-test --monitor

      # Process test set with custom context
      idp-cli process --stack-name my-stack --test-set fcc-example-test --context "Experiment v2.1" --monitor

      # Process test set with limited files for quick testing
      idp-cli process --stack-name my-stack --test-set fcc-example-test --number-of-files 5 --monitor

      # Process with specific configuration profile
      idp-cli process --stack-name my-stack --dir ./documents/ --config-profile v2 --monitor

      # Process manifest with baselines (automatically creates "idp-cli" test set for Test Studio integration)
      idp-cli process --stack-name my-stack --manifest docs_with_baselines.csv --monitor
    """
    return _process_impl(
        stack_name,
        manifest,
        directory,
        s3_uri,
        test_set,
        context,
        batch_id,
        file_pattern,
        recursive,
        config,
        batch_prefix,
        monitor,
        refresh_interval,
        region,
        number_of_files,
        config_version,
        config_revision,
    )


@cli.command(name="reprocess")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--step",
    required=True,
    type=click.Choice(["classification", "extraction"]),
    help="Pipeline step to rerun from",
)
@click.option(
    "--document-ids",
    help="Comma-separated list of document IDs to reprocess",
)
@click.option(
    "--batch-id",
    help="Batch ID to get document IDs from (alternative to --document-ids)",
)
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.option("--monitor", is_flag=True, help="Monitor progress until completion")
@click.option(
    "--refresh-interval",
    default=5,
    type=int,
    help="Seconds between status checks (default: 5)",
)
@click.option("--region", help="AWS region (optional)")
def reprocess(
    stack_name: str,
    step: str,
    document_ids: Optional[str],
    batch_id: Optional[str],
    force: bool,
    monitor: bool,
    refresh_interval: int,
    region: Optional[str],
):
    """
    Reprocess documents from a specific step
    
    Reprocesses documents already in InputBucket, leveraging existing OCR data.
    
    Steps:
      - classification: Reruns classification and all subsequent steps
      - extraction: Reruns extraction and assessment (keeps classification)
    
    Document ID Format: Use the S3 key format (e.g., "batch-id/document.pdf")
    
    Examples:
    
      # Rerun classification for specific documents
      idp-cli reprocess \\
          --stack-name my-stack \\
          --step classification \\
          --document-ids "batch-123/doc1.pdf,batch-123/doc2.pdf" \\
          --monitor
      
      # Rerun extraction for all documents in a batch
      idp-cli reprocess \\
          --stack-name my-stack \\
          --step extraction \\
          --batch-id cli-batch-20251015-143000 \\
          --monitor
    """
    # Call the existing rerun_inference implementation
    return rerun_inference(
        stack_name,
        step,
        document_ids,
        batch_id,
        force,
        monitor,
        refresh_interval,
        region,
    )


# Backward compatibility alias for run_inference
@cli.command(name="run-inference")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--manifest",
    type=click.Path(exists=True),
    help="Path to manifest file (CSV or JSON)",
)
@click.option(
    "--dir",
    "directory",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Local directory containing documents to process",
)
@click.option("--s3-uri", help="S3 URI to process (e.g., s3://bucket/prefix/)")
@click.option("--test-set", help="Test set ID to process from test set bucket")
@click.option(
    "--context", help="Context description for test run (used with --test-set)"
)
@click.option(
    "--batch-id",
    help="Custom batch ID (auto-generated if not provided, ignored with --test-set)",
)
@click.option(
    "--file-pattern",
    default="*.pdf",
    help="File pattern for directory/S3 scanning (default: *.pdf)",
)
@click.option(
    "--recursive/--no-recursive",
    default=True,
    help="Include subdirectories when scanning (default: recursive)",
)
@click.option(
    "--config",
    type=click.Path(exists=True),
    help="Path to configuration YAML file (optional)",
)
@click.option(
    "--batch-prefix",
    default="cli-batch",
    help="Batch ID prefix (used only if --batch-id not provided, default: cli-batch)",
)
@click.option("--monitor", is_flag=True, help="Monitor progress until completion")
@click.option(
    "--refresh-interval",
    default=5,
    type=int,
    help="Seconds between status checks (default: 5)",
)
@click.option("--region", help="AWS region (optional)")
@click.option(
    "--number-of-files",
    type=int,
    help="Limit number of files to process (for testing purposes)",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    help="Configuration profile to use for processing (e.g., v1, v2); --config-version is the former name and still works",
)
@click.option(
    "--config-revision",
    type=int,
    help=(
        "Revision of --config-profile to pin (e.g. 7). Omit to process under the "
        "profile's current configuration; pinning reproduces exactly what that "
        "revision recorded."
    ),
)
def run_inference(
    stack_name: str,
    manifest: Optional[str],
    directory: Optional[str],
    s3_uri: Optional[str],
    test_set: Optional[str],
    context: Optional[str],
    batch_id: Optional[str],
    file_pattern: str,
    recursive: bool,
    config: Optional[str],
    batch_prefix: str,
    monitor: bool,
    refresh_interval: int,
    region: Optional[str],
    number_of_files: Optional[int],
    config_version: Optional[str],
    config_revision: Optional[int],
):
    """
    Run inference on a batch of documents

    ⚠️  DEPRECATED: This command is maintained for backward compatibility.
    Please use 'idp-cli process' instead for new workflows.

    Equivalent command:
      idp-cli process --stack-name <stack> --manifest <file> [options]
    """
    return _process_impl(
        stack_name,
        manifest,
        directory,
        s3_uri,
        test_set,
        context,
        batch_id,
        file_pattern,
        recursive,
        config,
        batch_prefix,
        monitor,
        refresh_interval,
        region,
        number_of_files,
        config_version,
        config_revision,
    )


@cli.command(name="rerun-inference")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--step",
    required=True,
    type=click.Choice(["classification", "extraction"]),
    help="Pipeline step to rerun from",
)
@click.option(
    "--document-ids",
    help="Comma-separated list of document IDs to reprocess",
)
@click.option(
    "--batch-id",
    help="Batch ID to get document IDs from (alternative to --document-ids)",
)
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.option("--monitor", is_flag=True, help="Monitor progress until completion")
@click.option(
    "--refresh-interval",
    default=5,
    type=int,
    help="Seconds between status checks (default: 5)",
)
@click.option("--region", help="AWS region (optional)")
def rerun_inference(
    stack_name: str,
    step: str,
    document_ids: Optional[str],
    batch_id: Optional[str],
    force: bool,
    monitor: bool,
    refresh_interval: int,
    region: Optional[str],
):
    """
    DEPRECATED: Use 'reprocess' instead
    """
    # Call the new reprocess implementation
    return _rerun_inference_impl(
        stack_name,
        step,
        document_ids,
        batch_id,
        force,
        monitor,
        refresh_interval,
        region,
    )


# Implementation moved from rerun_inference - now used by re_process
def _rerun_inference_impl(
    stack_name: str,
    step: str,
    document_ids: Optional[str],
    batch_id: Optional[str],
    force: bool,
    monitor: bool,
    refresh_interval: int,
    region: Optional[str],
):
    try:
        # Validate mutually exclusive options
        if not document_ids and not batch_id:
            console.print(
                "[red]✗ Error: Must specify either --document-ids or --batch-id[/red]"
            )
            sys.exit(1)

        if document_ids and batch_id:
            console.print(
                "[red]✗ Error: Cannot specify both --document-ids and --batch-id[/red]"
            )
            sys.exit(1)

        from idp_sdk import IDPClient

        console.print(
            f"[bold blue]Initializing reprocess for stack: {stack_name}[/bold blue]"
        )
        client = IDPClient(stack_name=stack_name, region=region)

        # Get document count for confirmation display
        if document_ids:
            doc_id_list = [doc_id.strip() for doc_id in document_ids.split(",")]
            console.print(f"Processing {len(doc_id_list)} specified documents")
            reprocess_doc_ids = doc_id_list
            reprocess_batch_id = None
        else:
            console.print(f"Getting document IDs from batch: {batch_id}")
            # Pre-fetch IDs for count display (SDK will re-fetch internally if batch_id passed)
            doc_id_list = client.batch.get_document_ids(batch_id)
            console.print(f"Found {len(doc_id_list)} documents in batch")
            reprocess_doc_ids = doc_id_list
            reprocess_batch_id = None  # Pass explicit list so SDK doesn't re-fetch

        # Show what will be cleared based on step
        console.print()
        console.print(f"[bold yellow]⚠️  Rerun Step: {step}[/bold yellow]")
        console.print("━" * 60)

        if step == "classification":
            console.print("[bold]What will be cleared:[/bold]")
            console.print("  • All page classifications")
            console.print("  • All document sections")
            console.print("  • All extraction results")
            console.print()
            console.print("[bold]What will be kept:[/bold]")
            console.print("  • OCR data (pages, images, text)")
        else:  # extraction
            console.print("[bold]What will be cleared:[/bold]")
            console.print("  • Section extraction results")
            console.print("  • Section attributes")
            console.print()
            console.print("[bold]What will be kept:[/bold]")
            console.print("  • OCR data (pages, images, text)")
            console.print("  • Page classifications")
            console.print("  • Document sections structure")

        console.print("━" * 60)
        console.print()

        # Confirmation unless --force
        if not force:
            if not click.confirm(
                f"Reprocess {len(doc_id_list)} documents from {step} step?",
                default=True,
            ):
                console.print("[yellow]Rerun cancelled[/yellow]")
                return

        # Perform reprocess via SDK
        console.print()
        with console.status(
            f"[bold green]Reprocessing {len(doc_id_list)} documents..."
        ):
            result = client.batch.reprocess(
                step=step,
                document_ids=reprocess_doc_ids,
                batch_id=reprocess_batch_id,
            )

        # Show results
        console.print()
        if result.documents_queued > 0:
            console.print(
                f"[green]✓ Queued {result.documents_queued} documents for {step} reprocessing[/green]"
            )

        if result.documents_failed > 0:
            console.print(
                f"[red]✗ Failed to queue {result.documents_failed} documents[/red]"
            )
            for failed in result.failed_documents:
                console.print(f"  • {failed['object_key']}: {failed['error']}")

        console.print()

        if monitor and result.documents_queued > 0:
            _monitor_progress(
                client=client,
                batch_id=batch_id or "rerun",
                refresh_interval=refresh_interval,
            )

    except Exception as e:
        logger.error(f"Error rerunning documents: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option("--batch-id", help="Batch identifier or PK substring to search for")
@click.option("--document-id", help="Single document ID (alternative to --batch-id)")
@click.option(
    "--object-status",
    help="Filter by object status (e.g., COMPLETED, FAILED, QUEUED, RUNNING)",
)
@click.option("--wait", is_flag=True, help="Wait for all documents to complete")
@click.option(
    "--refresh-interval",
    default=5,
    type=int,
    help="Seconds between status checks (default: 5)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format: table (default) or json",
)
@click.option(
    "--show-details",
    is_flag=True,
    help="Show detailed information about matching documents",
)
@click.option(
    "--get-time",
    is_flag=True,
    help="Calculate and display timing statistics (processing time, queue time, etc.)",
)
@click.option(
    "--include-metering",
    is_flag=True,
    help="Include Lambda metering statistics (GB-seconds by stage) when using --get-time",
)
@click.option("--region", help="AWS region (optional)")
def status(
    stack_name: str,
    batch_id: Optional[str],
    document_id: Optional[str],
    object_status: Optional[str],
    wait: bool,
    refresh_interval: int,
    output_format: str,
    show_details: bool,
    get_time: bool,
    include_metering: bool,
    region: Optional[str],
):
    """
    Check status of documents by batch ID, document ID, or search criteria

    Specify ONE of:
      --batch-id: Search for documents with PK containing this substring
      --document-id: Check status of a single document

    Optional filters and display options:
      --object-status: Filter by status (COMPLETED, FAILED, QUEUED, RUNNING)
      --show-details: Show detailed document information
      --get-time: Calculate timing statistics
      --include-metering: Include Lambda metering data (requires --get-time)

    Examples:

      # Search for all documents in a batch (PK substring search)
      idp-cli status --stack-name my-stack --batch-id cli-batch-20250110-153045-abc12345

      # Search for completed documents in a batch
      idp-cli status --stack-name my-stack --batch-id batch-123 --object-status COMPLETED

      # Search with timing statistics
      idp-cli status --stack-name my-stack --batch-id batch-123 --object-status COMPLETED --get-time

      # Search with timing and Lambda metering
      idp-cli status --stack-name my-stack --batch-id test --object-status COMPLETED --get-time --include-metering

      # Check single document status
      idp-cli status --stack-name my-stack --document-id batch-123/invoice.pdf

      # Monitor documents until completion
      idp-cli status --stack-name my-stack --batch-id batch-123 --wait

      # Get JSON output for scripting
      idp-cli status --stack-name my-stack --batch-id batch-123 --format json
    """
    try:
        from .search_tracking_table import TrackingTableSearcher

        # Validate mutually exclusive options
        if not batch_id and not document_id:
            console.print(
                "[red]✗ Error: Must specify either --batch-id or --document-id[/red]"
            )
            sys.exit(1)

        if batch_id and document_id:
            console.print(
                "[red]✗ Error: Cannot specify both --batch-id and --document-id[/red]"
            )
            sys.exit(1)

        # Get document IDs to monitor
        if batch_id:
            # Use TrackingTableSearcher for PK substring search
            searcher = TrackingTableSearcher(stack_name=stack_name, region=region)

            # Default to searching all statuses if not specified
            if object_status:
                # Search with specific status filter
                search_results = searcher.search_by_pk_and_status(
                    pk=batch_id, object_status=object_status
                )
            else:
                # Search across all statuses by doing multiple searches
                # This ensures we get all documents matching the PK substring
                all_statuses = [
                    "COMPLETED",
                    "FAILED",
                    "QUEUED",
                    "RUNNING",
                    "PROCESSING",
                ]
                all_items = []

                console.print(
                    f"[yellow]Searching for documents with PK containing '{batch_id}'...[/yellow]"
                )

                for status in all_statuses:
                    results = searcher.search_by_pk_and_status(
                        pk=batch_id, object_status=status
                    )
                    if results.get("success") and results.get("items"):
                        all_items.extend(results["items"])

                search_results = {
                    "success": True,
                    "count": len(all_items),
                    "items": all_items,
                    "pk": batch_id,
                    "object_status": "ALL",
                }

                console.print(
                    f"[green]✓ Found {len(all_items)} matching documents[/green]"
                )

            if not search_results.get("success"):
                console.print(
                    f"[red]✗ Search failed: {search_results.get('error')}[/red]"
                )
                sys.exit(1)

            if search_results.get("count", 0) == 0:
                msg = f"No documents found matching batch-id '{batch_id}'"
                if object_status:
                    msg += f" with status '{object_status}'"
                console.print(f"[yellow]{msg}[/yellow]")
                sys.exit(1)

            # Extract document IDs from search results
            document_ids = []
            for item in search_results.get("items", []):
                # Extract ObjectKey from DynamoDB format
                object_key = item.get("ObjectKey", {}).get("S")
                if object_key:
                    document_ids.append(object_key)

            identifier = batch_id

            # Display search results summary if not waiting
            if not wait and not get_time:
                console.print()
                console.print(f"[bold blue]Search Results for: {batch_id}[/bold blue]")
                if object_status:
                    console.print(f"[dim]Status filter: {object_status}[/dim]")
                console.print(f"[dim]Documents found: {len(document_ids)}[/dim]")
                console.print()

                # Show details if requested
                if show_details:
                    searcher.display_results(search_results, show_details=True)
                    console.print()

        else:
            # Single document
            document_ids = [document_id]
            identifier = document_id

        # Handle timing statistics display (only for batch-id searches)
        if get_time and batch_id:
            console.print()
            timing_stats = searcher.calculate_timing_statistics(
                search_results, include_metering=include_metering
            )
            searcher.display_timing_statistics(timing_stats)

            # If not waiting, we're done
            if not wait:
                sys.exit(0)

            console.print()

        if wait:
            # JSON format not compatible with live monitoring
            if output_format == "json":
                console.print(
                    "[yellow]Warning: --format json ignored with --wait (using table display for live monitoring)[/yellow]"
                )
                console.print()

            from idp_sdk import IDPClient as _IDPClient

            _client = _IDPClient(stack_name=stack_name, region=region)
            # Monitor until completion
            _monitor_progress(
                client=_client,
                batch_id=identifier,
                refresh_interval=refresh_interval,
            )
        else:
            # Show current status once via IDPClient
            from idp_sdk import IDPClient as _IDPClient

            _client = _IDPClient(stack_name=stack_name, region=region)
            batch_status = _client.batch.get_status(identifier)
            status_data, stats = _batch_status_to_display_dicts(batch_status)

            if output_format == "json":
                # JSON output for programmatic use
                json_output = display.format_status_json(status_data, stats)
                console.print(json_output)

                # Determine exit code from JSON
                import json as json_module

                result = json_module.loads(json_output)
                sys.exit(result.get("exit_code", 2))
            else:
                # Table output for human viewing
                console.print()
                if batch_id:
                    console.print(f"[bold blue]Batch: {batch_id}[/bold blue]")
                else:
                    console.print(f"[bold blue]Document: {document_id}[/bold blue]")

                display.display_status_table(status_data)

                # Show statistics
                console.print(display.create_statistics_panel(stats))

                # Show final status summary and exit with appropriate code
                exit_code = display.show_final_status_summary(status_data, stats)
                sys.exit(exit_code)

    except Exception as e:
        logger.error(f"Error checking status: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option("--limit", default=10, type=int, help="Maximum number of batches to list")
@click.option("--region", help="AWS region (optional)")
def list_batches(stack_name: str, limit: int, region: Optional[str]):
    """
    List recent batch processing jobs

    Example:

      idp-cli list-batches --stack-name my-stack --limit 5
    """
    try:
        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)
        result = client.batch.list(limit=limit)

        if not result.batches:
            console.print("[yellow]No batches found[/yellow]")
            return

        # Create table
        table = Table(title=f"Recent Batches (Last {limit})", show_header=True)
        table.add_column("Batch ID", style="cyan")
        table.add_column("Documents", justify="right")
        table.add_column("Queued", justify="right")
        table.add_column("Failed", justify="right")
        table.add_column("Timestamp")

        for batch in result.batches:
            table.add_row(
                batch.batch_id,
                str(len(batch.document_ids)),
                str(batch.queued),
                str(batch.failed),
                batch.timestamp[:19],  # Trim timestamp
            )

        console.print()
        console.print(table)
        console.print()

    except Exception as e:
        logger.error(f"Error listing batches: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--batch-id",
    help="Batch identifier (mutually exclusive with --document-id/--run-id)",
)
@click.option(
    "--document-id",
    help="Document object key — required with --run-id to download a specific version",
)
@click.option(
    "--run-id",
    help="Version run id (from `idp-cli list-versions`). Downloads the exact "
    "pinned bytes of that processing run. Requires --document-id.",
)
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(),
    help="Output directory for downloaded results",
)
@click.option(
    "--file-types",
    default="all",
    help="File types to download: pages, sections, summary, evaluation, or 'all' (default: all)",
)
@click.option("--region", help="AWS region (optional)")
def download_results(
    stack_name: str,
    batch_id: Optional[str],
    document_id: Optional[str],
    run_id: Optional[str],
    output_dir: str,
    file_types: str,
    region: Optional[str],
):
    """
    Download processing results from OutputBucket

    Examples:

      # Download all results for a batch
      idp-cli download-results --stack-name my-stack --batch-id cli-batch-20251015-143000 --output-dir ./results/

      # Download only extraction results (sections)
      idp-cli download-results --stack-name my-stack --batch-id <id> --output-dir ./results/ --file-types sections

      # Download a specific document VERSION (exact bytes of one processing run)
      idp-cli download-results --stack-name my-stack --document-id loan-123/package.pdf \\
          --run-id 20250707T141530Z-exec-abc --output-dir ./results/
    """
    try:
        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)

        # Version download path: pinned S3 object versions from a run manifest.
        if run_id:
            if not document_id:
                console.print("[red]✗ --run-id requires --document-id[/red]")
                sys.exit(1)
            console.print(
                f"[bold blue]Downloading version {run_id} of {document_id}[/bold blue]"
            )
            result = client.batch.download_version(
                document_id=document_id, run_id=run_id, output_dir=output_dir
            )
            console.print(
                f"\n[green]✓ Downloaded {result.files_downloaded} files to "
                f"{result.output_dir}[/green]"
            )
            console.print()
            return

        if not batch_id:
            console.print(
                "[red]✗ Provide either --batch-id or --document-id/--run-id[/red]"
            )
            sys.exit(1)

        console.print(
            f"[bold blue]Downloading results for batch: {batch_id}[/bold blue]"
        )

        # Parse file types
        if file_types == "all":
            types_list = ["all"]
        else:
            types_list = [t.strip() for t in file_types.split(",")]

        # Download results
        result = client.batch.download_results(
            batch_id=batch_id, output_dir=output_dir, file_types=types_list
        )

        console.print(
            f"\n[green]✓ Downloaded {result.files_downloaded} files to {output_dir}[/green]"
        )
        console.print(f"  Documents: {result.documents_downloaded}")
        console.print(f"  Output: {output_dir}/{batch_id}/")
        console.print()

    except Exception as e:
        logger.error(f"Error downloading results: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="use-as-baseline")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--document-id",
    required=True,
    help="Document object key (S3 key) of a processed document, e.g. "
    "'loan-123/package.pdf'",
)
@click.option("--region", help="AWS region (optional)")
def use_as_baseline(
    stack_name: str,
    document_id: str,
    region: Optional[str],
):
    """
    Promote a processed document's output to the evaluation baseline.

    Programmatic equivalent of the UI "Use as Evaluation Baseline" button:
    copies the document's output into the evaluation baseline bucket and sets
    its EvaluationStatus to BASELINE_AVAILABLE. Runs synchronously.

    Examples:

      idp-cli use-as-baseline --stack-name my-stack --document-id loan-123/package.pdf
    """
    try:
        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)

        console.print(
            f"[bold blue]Copying '{document_id}' to evaluation baseline...[/bold blue]"
        )

        result = client.evaluation.use_as_baseline(document_id=document_id)

        console.print(f"\n[green]✓ Baseline created for {result.document_id}[/green]")
        console.print(f"  Files copied: {result.files_copied}")
        console.print(f"  Evaluation status: {result.evaluation_status}")
        console.print()

    except Exception as e:
        logger.error(f"Error using document as baseline: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--document-id", required=True, help="Document object key (its tracking id)"
)
@click.option("--region", help="AWS region (optional)")
def list_versions(stack_name: str, document_id: str, region: Optional[str]):
    """
    List retained processing-run versions for a document, newest first.

    Each successful processing run of a document is retained as a version whose
    output bytes are pinned by S3 object VersionId. Use the RunId with
    `download-results --run-id` to fetch that exact version.

    Example:

      idp-cli list-versions --stack-name my-stack --document-id loan-123/package.pdf
    """
    try:
        from rich.table import Table

        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)
        versions = client.batch.list_versions(document_id=document_id)

        if not versions:
            console.print(f"[yellow]No versions found for {document_id}[/yellow]")
            return

        table = Table(title=f"Versions for {document_id}")
        table.add_column("Run ID")
        table.add_column("Completed")
        table.add_column("Config Profile")
        table.add_column("Pages", justify="right")
        table.add_column("Files", justify="right")

        for v in versions:
            table.add_row(
                str(v.get("RunId", "")),
                str(v.get("CompletionTime", ""))[:19],
                str(v.get("ConfigVersion", "N/A")),
                str(v.get("PageCount", "-")),
                str(v.get("FileCount", "-")),
            )

        console.print()
        console.print(table)
        console.print()

    except Exception as e:
        logger.error(f"Error listing versions: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.option(
    "--dir",
    "directory",
    type=click.Path(exists=True, file_okay=False),
    help="Local directory to scan",
)
@click.option("--s3-uri", help="S3 URI to scan (e.g., s3://bucket/prefix/)")
@click.option(
    "--baseline-dir",
    type=click.Path(exists=True, file_okay=False),
    help="Baseline directory to auto-match (only with --dir)",
)
@click.option(
    "--output",
    type=click.Path(),
    help="Output manifest file path (CSV) - optional when using --test-set",
)
@click.option("--file-pattern", default="*.pdf", help="File pattern (default: *.pdf)")
@click.option(
    "--recursive/--no-recursive",
    default=True,
    help="Include subdirectories (default: recursive)",
)
@click.option("--region", help="AWS region (optional)")
@click.option(
    "--test-set",
    help="Test set name - creates folder in test set bucket and uploads files (backend generates ID)",
)
@click.option(
    "--stack-name", help="CloudFormation stack name (required with --test-set)"
)
def generate_manifest(
    directory: Optional[str],
    s3_uri: Optional[str],
    baseline_dir: Optional[str],
    output: Optional[str],
    file_pattern: str,
    recursive: bool,
    region: Optional[str],
    test_set: Optional[str],
    stack_name: Optional[str],
):
    """
    Generate a manifest file from directory or S3 URI

    The manifest can then be edited to add baseline_source or customize document_id values.
    Use --baseline-dir to automatically match baseline directories by document ID.
    Use --test-set to upload files to test set bucket and create test set folder structure.

    Examples:

      # Generate from local directory
      idp-cli generate-manifest --dir ./documents/ --output manifest.csv

      # With automatic baseline matching
      idp-cli generate-manifest --dir ./documents/ --baseline-dir ./baselines/ --output manifest.csv

      # Generate from S3 URI
      idp-cli generate-manifest --s3-uri s3://bucket/prefix/ --output manifest.csv

      # With file pattern
      idp-cli generate-manifest --dir ./docs/ --output manifest.csv --file-pattern "W2*.pdf"

      # Create test set and upload files (output optional) - use test set name
      idp-cli generate-manifest --dir ./documents/ --baseline-dir ./baselines/ --test-set "fcc example test" --stack-name IDP

      # Create test set with baseline matching and manifest output
      idp-cli generate-manifest --dir ./documents/ --baseline-dir ./baselines/ --test-set "fcc example test" --stack-name IDP --output manifest.csv
    """
    try:
        import csv
        import os

        # Validate test set requirements
        if test_set and not stack_name:
            console.print(
                "[red]✗ Error: --stack-name is required when using --test-set[/red]"
            )
            sys.exit(1)

        if test_set and not baseline_dir:
            console.print(
                "[red]✗ Error: --baseline-dir is required when using --test-set[/red]"
            )
            sys.exit(1)

        if test_set and s3_uri:
            console.print(
                "[red]✗ Error: --test-set requires --dir (not --s3-uri) to work with --baseline-dir[/red]"
            )
            sys.exit(1)

        # Validate output requirements
        if not test_set and not output:
            console.print(
                "[red]✗ Error: --output is required when not using --test-set[/red]"
            )
            sys.exit(1)

        # Validate mutually exclusive options
        if not directory and not s3_uri:
            console.print("[red]✗ Error: Must specify either --dir or --s3-uri[/red]")
            sys.exit(1)
        if directory and s3_uri:
            console.print("[red]✗ Error: Cannot specify both --dir and --s3-uri[/red]")
            sys.exit(1)

        # Import here to avoid circular dependency during scanning

        documents = []

        # Initialize test set bucket info if needed
        test_set_bucket = None
        s3_client = None
        if test_set:
            import boto3

            _client = IDPClient(stack_name=stack_name, region=region)
            resources = _client._get_stack_resources(stack_name)
            test_set_bucket = resources.get("TestSetBucket")
            if not test_set_bucket:
                console.print(
                    "[red]✗ Error: TestSetBucket not found in stack resources[/red]"
                )
                sys.exit(1)

            s3_client = boto3.client("s3", region_name=region)
            console.print(f"[bold blue]Test set bucket: {test_set_bucket}[/bold blue]")

        if directory:
            console.print(f"[bold blue]Scanning directory: {directory}[/bold blue]")

            # Import scan method directly
            import glob as glob_module

            dir_path = os.path.abspath(directory)
            if recursive:
                search_pattern = os.path.join(dir_path, "**", file_pattern)
            else:
                search_pattern = os.path.join(dir_path, file_pattern)

            for file_path in glob_module.glob(search_pattern, recursive=recursive):
                if os.path.isfile(file_path):
                    documents.append({"document_path": file_path})
        else:  # s3_uri
            console.print(f"[bold blue]Scanning S3 URI: {s3_uri}[/bold blue]")

            # Parse S3 URI
            if not s3_uri.startswith("s3://"):
                console.print("[red]✗ Error: Invalid S3 URI[/red]")
                sys.exit(1)

            uri_parts = s3_uri[5:].split("/", 1)
            bucket = uri_parts[0]
            prefix = uri_parts[1] if len(uri_parts) > 1 else ""

            # List S3 objects
            import fnmatch

            import boto3

            s3 = boto3.client("s3", region_name=region)
            paginator = s3.get_paginator("list_objects_v2")

            if prefix and not prefix.endswith("/"):
                prefix = prefix + "/"

            pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]

                    if key.endswith("/"):
                        continue

                    if not recursive:
                        rel_key = key[len(prefix) :]
                        if "/" in rel_key:
                            continue

                    filename = os.path.basename(key)
                    if not fnmatch.fnmatch(filename, file_pattern):
                        continue

                    full_uri = f"s3://{bucket}/{key}"

                    documents.append({"document_path": full_uri})

        if not documents:
            console.print("[yellow]No documents found[/yellow]")
            sys.exit(1)

        console.print(f"Found {len(documents)} documents")

        # Match baselines if baseline_dir provided
        baseline_map = {}
        if baseline_dir:
            if s3_uri:
                console.print(
                    "[yellow]Warning: --baseline-dir only works with --dir, ignoring[/yellow]"
                )
            else:
                console.print(
                    f"[bold blue]Matching baselines from: {baseline_dir}[/bold blue]"
                )

                import os

                baseline_path = os.path.abspath(baseline_dir)

                # Scan for baseline subdirectories
                for item in os.listdir(baseline_path):
                    item_path = os.path.join(baseline_path, item)
                    if os.path.isdir(item_path):
                        baseline_map[item] = item_path

                console.print(f"Found {len(baseline_map)} baseline directories")

                # Show matching statistics
                matched = 0
                for doc in documents:
                    filename = os.path.basename(doc["document_path"])
                    if filename in baseline_map:
                        matched += 1

                console.print(
                    f"Matched {matched}/{len(documents)} documents to baselines"
                )
                console.print()

        # Upload to test set bucket if test_set is specified
        if test_set:
            # Check if test set already exists
            try:
                response = s3_client.list_objects_v2(
                    Bucket=test_set_bucket, Prefix=f"{test_set}/", MaxKeys=1
                )
                if response.get("Contents"):
                    console.print(
                        f"[yellow]Warning: Test set '{test_set}' already exists in bucket[/yellow]"
                    )
                    console.print(
                        "[yellow]Files will be overwritten. Continue? [y/N][/yellow]",
                        end=" ",
                    )

                    response = input().strip().lower()
                    if response not in ["y", "yes"]:
                        console.print("[red]✗ Aborted[/red]")
                        sys.exit(1)
            except Exception as e:
                console.print(
                    f"[yellow]Warning: Could not check existing test set: {e}[/yellow]"
                )

            console.print(
                f"[bold blue]Uploading files to test set: {test_set}[/bold blue]"
            )

            # Clear existing test set folder if it exists
            try:
                response = s3_client.list_objects_v2(
                    Bucket=test_set_bucket, Prefix=f"{test_set}/"
                )

                if "Contents" in response:
                    # Delete all existing objects in the test set folder
                    objects_to_delete = [
                        {"Key": obj["Key"]} for obj in response["Contents"]
                    ]

                    if objects_to_delete:
                        s3_client.delete_objects(
                            Bucket=test_set_bucket,
                            Delete={"Objects": objects_to_delete},
                        )
                        console.print(
                            f"  Cleared {len(objects_to_delete)} existing files"
                        )

            except Exception as e:
                console.print(
                    f"[yellow]Warning: Could not clear existing files: {e}[/yellow]"
                )

            # Place .uploading marker to prevent resolver race condition
            # The test set resolver's auto-detection skips folders with this marker,
            # preventing premature validation before all files are uploaded.
            # See: https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/193
            marker_key = f"{test_set}/.uploading"
            s3_client.put_object(
                Bucket=test_set_bucket, Key=marker_key, Body=b"upload-in-progress"
            )

            # Upload input documents
            for i, doc in enumerate(documents):
                doc_path = doc["document_path"]
                filename = os.path.basename(doc_path)
                s3_key = f"{test_set}/input/{filename}"

                s3_client.upload_file(doc_path, test_set_bucket, s3_key)
                doc["document_path"] = f"s3://{test_set_bucket}/{s3_key}"
                console.print(f"  Uploaded input {i + 1}/{len(documents)}: {filename}")

            # Upload baseline files
            for filename, baseline_path in baseline_map.items():
                # Upload all files in the baseline directory recursively
                import glob as glob_module
                import os

                baseline_files = glob_module.glob(
                    os.path.join(baseline_path, "**", "*"), recursive=True
                )
                for baseline_file in baseline_files:
                    if os.path.isfile(baseline_file):
                        # Preserve directory structure relative to baseline_path
                        rel_path = os.path.relpath(baseline_file, baseline_path)
                        s3_key = f"{test_set}/baseline/{filename}/{rel_path}"
                        s3_client.upload_file(baseline_file, test_set_bucket, s3_key)

                # Update baseline_map to point to S3 location
                baseline_map[filename] = (
                    f"s3://{test_set_bucket}/{test_set}/baseline/{filename}/"
                )
                console.print(f"  Uploaded baseline: {filename}")

        # Write manifest (2 columns only)
        if output:
            with open(output, "w", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["document_path", "baseline_source"]
                )
                writer.writeheader()
                for doc in documents:
                    # Match baseline using full filename (including extension)
                    filename = os.path.basename(doc["document_path"])
                    baseline_source = baseline_map.get(filename, "")

                    writer.writerow(
                        {
                            "document_path": doc["document_path"],
                            "baseline_source": baseline_source,
                        }
                    )

            console.print(f"[green]✓ Generated manifest: {output}[/green]")
            console.print()

        if test_set:
            # Remove .uploading marker now that all files are uploaded
            marker_key = f"{test_set}/.uploading"
            try:
                s3_client.delete_object(Bucket=test_set_bucket, Key=marker_key)
            except Exception as e:
                console.print(
                    f"[yellow]Warning: Could not remove upload marker: {e}[/yellow]"
                )

            # Auto-register test set in tracking table
            _client2 = IDPClient(stack_name=stack_name, region=region)
            resources = _client2._get_stack_resources(stack_name)
            _invoke_test_set_resolver(stack_name, test_set, region, resources)

            console.print(
                f"[green]✓ Test set '{test_set}' created successfully[/green]"
            )
            console.print(f"  Input files: s3://{test_set_bucket}/{test_set}/input/")
            console.print(
                f"  Baseline files: s3://{test_set_bucket}/{test_set}/baseline/"
            )
            console.print()
            console.print("[bold]Next Steps: Run inference[/bold]")
            console.print(
                f"  - Using test set: [cyan]idp-cli process --test-set {test_set} --stack-name {stack_name} --monitor[/cyan]"
            )
            console.print(
                f"  - With limited files: [cyan]idp-cli process --test-set {test_set} --stack-name {stack_name} --number-of-files {{N}} --monitor[/cyan]"
            )
            if output:
                console.print(
                    f"  - Using manifest: [cyan]idp-cli process --stack-name {stack_name} --manifest {output} --monitor[/cyan]"
                )
                console.print(
                    f"  - With limited files: [cyan]idp-cli process --stack-name {stack_name} --manifest {output} --number-of-files {{N}} --monitor[/cyan]"
                )
        elif baseline_map:
            console.print("[bold]Baseline matching complete[/bold]")
            console.print("Ready to process with evaluations!")
        else:
            console.print("[bold]Next steps:[/bold]")
            console.print(
                "  1. Edit manifest to add baseline_source or customize document_id"
            )
            if output:
                console.print(
                    f"  2. Process: [cyan]idp-cli process --stack-name <stack> --manifest {output}[/cyan]"
                )
        console.print()

    except Exception as e:
        logger.error(f"Error generating manifest: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="validate-manifest")
@click.option(
    "--manifest",
    required=True,
    type=click.Path(exists=True),
    help="Path to manifest file to validate",
)
def validate_manifest_cmd(manifest: str):
    """
    Validate a manifest file without processing

    Example:

      idp-cli validate-manifest --manifest documents.csv
    """
    try:
        _client = IDPClient()
        result = _client.manifest.validate(manifest_path=manifest)
        is_valid = result.valid
        error = result.error

        if is_valid:
            console.print(f"[green]✓ Manifest is valid: {manifest}[/green]")
        else:
            console.print("[red]✗ Manifest validation failed:[/red]")
            console.print(f"  {error}")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error validating manifest: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


def _batch_status_to_display_dicts(batch_status):
    """
    Convert a BatchStatus Pydantic model to the legacy dict format expected by display.py.

    display.py functions (create_live_display, show_final_summary, etc.) consume:
      status_data = {"completed": [...], "running": [...], "queued": [...], "failed": [...], "total": N}
      stats       = {"total": N, "completed": N, "failed": N, "running": N, "queued": N,
                     "all_complete": bool, "success_rate": float (0-100),
                     "completion_percentage": float, "avg_duration_seconds": float}
    """
    completed_docs = []
    running_docs = []
    queued_docs = []
    failed_docs = []

    total_duration = 0.0
    duration_count = 0

    for doc in batch_status.documents:
        doc_dict = {
            "document_id": doc.document_id,
            "status": doc.status,
            "start_time": doc.start_time or "",
            "end_time": doc.end_time or "",
            "duration": doc.duration_seconds or 0,
            "num_pages": doc.num_pages,
            "num_sections": doc.num_sections,
            "error": doc.error or "",
        }
        status_upper = (doc.status or "").upper()
        if status_upper == "COMPLETED":
            completed_docs.append(doc_dict)
            if doc.duration_seconds:
                total_duration += doc.duration_seconds
                duration_count += 1
        elif status_upper == "FAILED":
            failed_docs.append(doc_dict)
        elif status_upper in (
            "RUNNING",
            "CLASSIFYING",
            "EXTRACTING",
            "ASSESSING",
            "RULE_VALIDATION",
            "RULE_VALIDATION_ORCHESTRATOR",
            "SUMMARIZING",
            "HITL_IN_PROGRESS",
            "EVALUATING",
        ):
            running_docs.append(doc_dict)
        else:
            queued_docs.append(doc_dict)

    total = batch_status.total
    completed = len(completed_docs)
    failed = len(failed_docs)
    running = len(running_docs)
    queued = len(queued_docs)

    status_data = {
        "total": total,
        "completed": completed_docs,
        "running": running_docs,
        "queued": queued_docs,
        "failed": failed_docs,
    }

    avg_duration = total_duration / duration_count if duration_count > 0 else 0.0
    completion_pct = (completed + failed) / total * 100.0 if total > 0 else 0.0
    # SDK returns success_rate as 0.0–1.0; display expects 0–100
    success_rate = batch_status.success_rate * 100.0

    stats = {
        "total": total,
        "completed": completed,
        "failed": failed,
        "running": running,
        "queued": queued,
        "all_complete": batch_status.all_complete,
        "success_rate": success_rate,
        "completion_percentage": completion_pct,
        "avg_duration_seconds": avg_duration,
    }

    return status_data, stats


def _monitor_progress(
    client,
    batch_id: str,
    refresh_interval: int,
    # Legacy keyword arguments kept for backward-compat callers not yet migrated
    stack_name: Optional[str] = None,
    document_ids: Optional[list] = None,
    region: Optional[str] = None,
    resources: Optional[dict] = None,
):
    """
    Monitor batch progress with live updates using IDPClient.

    Args:
        client: IDPClient instance (preferred) OR pass stack_name+region for legacy callers
        batch_id: Batch identifier
        refresh_interval: Seconds between status checks
        stack_name: (legacy) CloudFormation stack name
        document_ids: (legacy, unused) kept for signature compatibility
        region: (legacy) AWS region
        resources: (legacy, unused) kept for signature compatibility
    """
    from idp_sdk import IDPClient as _IDPClient

    # Support legacy callers that still pass stack_name/region instead of a client
    if not isinstance(client, _IDPClient):
        # client arg was actually stack_name (positional from old callers)
        # Reconstruct: _monitor_progress(stack_name, batch_id, document_ids, ...)
        # Old signature: _monitor_progress(stack_name, batch_id, document_ids, refresh_interval, region, resources)
        # New callers pass client= as first arg; legacy code path below handles old callers
        _stack_name = stack_name or client
        idp_client = _IDPClient(stack_name=_stack_name, region=region)
    else:
        idp_client = client

    display.show_monitoring_header(batch_id)

    start_time = time.time()
    status_data = {}
    stats = {}

    # Minimum wait time before considering batch complete (seconds)
    # This gives time for documents to be picked up by the queue and tracked in DynamoDB
    MIN_WAIT_BEFORE_COMPLETE = 60

    try:
        with Live(console=console, refresh_per_second=1) as live:
            while True:
                # Get current status via SDK
                batch_status = idp_client.batch.get_status(batch_id)
                status_data, stats = _batch_status_to_display_dicts(batch_status)
                elapsed_time = time.time() - start_time

                # Update display (display.py receives legacy dict format)
                layout = display.create_live_display(
                    batch_id=batch_id,
                    status_data=status_data,
                    stats=stats,
                    elapsed_time=elapsed_time,
                )
                live.update(layout)

                # Check if all complete
                # Add grace period: don't exit early if no documents have completed/failed yet
                # This handles the case where documents are still being picked up by the queue
                if stats["all_complete"]:
                    # If we have actual completions or failures, we can exit
                    has_terminal_docs = stats["completed"] > 0 or stats["failed"] > 0
                    # Or if we've waited long enough (documents should have started by now)
                    waited_long_enough = elapsed_time >= MIN_WAIT_BEFORE_COMPLETE

                    if has_terminal_docs or waited_long_enough:
                        break
                    else:
                        # Documents haven't started yet, keep waiting
                        logger.debug(
                            f"all_complete=True but no terminal docs yet, waiting... "
                            f"(elapsed={elapsed_time:.1f}s, min_wait={MIN_WAIT_BEFORE_COMPLETE}s)"
                        )

                # Wait before next check
                time.sleep(refresh_interval)

    except KeyboardInterrupt:
        logger.info("Monitoring interrupted by user")
        console.print()
        console.print(
            "[yellow]Monitoring stopped. Processing continues in background.[/yellow]"
        )
        _sn = stack_name or (
            client
            if not isinstance(client, _IDPClient)
            else getattr(idp_client, "_stack_name", batch_id)
        )
        display.show_monitoring_instructions(_sn or batch_id, batch_id)
        return
    except Exception as e:
        logger.error(f"Monitoring error: {e}", exc_info=True)
        console.print()
        console.print(f"[red]Monitoring error: {e}[/red]")
        console.print("[yellow]You can check status later with:[/yellow]")
        _sn = stack_name or batch_id
        display.show_monitoring_instructions(_sn, batch_id)
        return

    # Show final summary
    logger.info("Showing final summary")
    elapsed_time = time.time() - start_time
    display.show_final_summary(status_data, stats, elapsed_time)


def _process_test_set(
    stack_name: str,
    test_set_name: str,
    context: Optional[str],
    region: Optional[str],
    client,
    number_of_files: Optional[int] = None,
    config_version: Optional[str] = None,
    config_revision: Optional[int] = None,
):
    """Common function to process test sets"""
    # Resolve resources dict from IDPClient for Lambda helper functions
    _resources_obj = client.stack.get_resources()
    resources = {
        "InputBucket": _resources_obj.input_bucket,
        "OutputBucket": _resources_obj.output_bucket,
        "DocumentsTable": _resources_obj.documents_table,
        "TestSetBucket": getattr(_resources_obj, "test_set_bucket", None),
        "StateMachineArn": getattr(_resources_obj, "state_machine_arn", None),
        "DocumentQueue": getattr(_resources_obj, "document_queue", None),
    }

    # Auto-detect test set using test_set_resolver lambda
    _invoke_test_set_resolver(stack_name, test_set_name, region, resources)

    # Invoke test runner lambda
    test_run_result = _invoke_test_runner(
        stack_name,
        test_set_name,
        context,
        region,
        resources,
        number_of_files,
        config_version,
        config_revision,
    )
    batch_id = test_run_result["testRunId"]

    # Get document IDs from test set for monitoring
    document_ids = _get_test_set_document_ids(
        stack_name, test_set_name, batch_id, region, resources
    )

    # If numberOfFiles was specified, limit document_ids to match actual queued count
    if (
        number_of_files is not None
        and len(document_ids) > test_run_result["filesCount"]
    ):
        document_ids = document_ids[: test_run_result["filesCount"]]

    # Create mock batch_result for monitoring
    batch_result = {
        "batch_id": batch_id,
        "documents_queued": test_run_result["filesCount"],
        "documents": [],  # Test runner handles document tracking
        "document_ids": document_ids,
        "uploaded": 0,  # No files uploaded by CLI for test sets
        "skipped": 0,
        "failed": 0,
        "queued": test_run_result["filesCount"],  # Files queued by test runner
    }

    return batch_result


def _invoke_test_set_resolver(
    stack_name: str, test_set_name: str, region: Optional[str], resources: dict
):
    """Invoke test set resolver lambda for auto-detection"""
    import json

    lambda_client = boto3.client("lambda", region_name=region)

    # Handle pagination to get all functions - EXACT same logic as test runner
    all_functions = []
    paginator = lambda_client.get_paginator("list_functions")
    for page in paginator.paginate():
        all_functions.extend(page["Functions"])

    # Match by the stack-name prefix + the unique function fragment. We do NOT
    # match the full "-APIRESOLVERSTACK-" nested-stack segment because
    # CloudFormation truncates long logical ids in physical names (e.g.
    # "<stack>-APIRESOLVE-TestSetResolverFunction-xxxx"), which a fixed prefix
    # would miss.
    test_set_resolver_function = next(
        (
            f["FunctionName"]
            for f in all_functions
            if f["FunctionName"].startswith(f"{stack_name}-APIRESOLVE")
            and "TestSetResolverFunction" in f["FunctionName"]
        ),
        None,
    )

    if not test_set_resolver_function:
        console.print(
            "[yellow]Warning: TestSetResolverFunction not found, skipping auto-detection[/yellow]"
        )
        return

    # Call getTestSets to trigger auto-detection and registration
    payload = {"info": {"fieldName": "getTestSets"}, "arguments": {}}

    console.print(f"[bold blue]Auto-detecting test set: {test_set_name}[/bold blue]")

    try:
        response = lambda_client.invoke(
            FunctionName=test_set_resolver_function, Payload=json.dumps(payload)
        )

        result = json.loads(response["Payload"].read())

        # Check for Lambda function errors (AWS returns StatusCode=200 for invocation success,
        # but function errors are indicated by errorMessage in the payload)
        if "errorMessage" in result:
            error_type = result.get("errorType", "Unknown")
            error_msg = result["errorMessage"]
            console.print(
                f"[yellow]Warning: Test set resolver failed ({error_type}): {error_msg}[/yellow]"
            )
            return

        if response["StatusCode"] == 200:
            console.print("[green]✓ Test set auto-detection completed[/green]")
        else:
            console.print(
                f"[yellow]Warning: Test set resolver invocation failed with status {response['StatusCode']}[/yellow]"
            )

    except Exception as e:
        console.print(f"[yellow]Warning: Could not auto-detect test set: {e}[/yellow]")


def _invoke_test_runner(
    stack_name: str,
    test_set: str,
    context: Optional[str],
    region: Optional[str],
    resources: dict,
    number_of_files: Optional[int] = None,
    config_version: Optional[str] = None,
    config_revision: Optional[int] = None,
):
    """Invoke test runner lambda to start test set processing"""
    import json

    # Find test runner function by name pattern
    # Match: <stack_name>-APIRESOLVE*-TestRunnerFunction-* (logical id is truncated)
    lambda_client = boto3.client("lambda", region_name=region)

    # Handle pagination to get all functions
    all_functions = []
    paginator = lambda_client.get_paginator("list_functions")
    for page in paginator.paginate():
        all_functions.extend(page["Functions"])

    # Stack prefix + function fragment (not the full "-APIRESOLVERSTACK-" segment,
    # which CloudFormation truncates in physical names).
    test_runner_function = next(
        (
            f["FunctionName"]
            for f in all_functions
            if f["FunctionName"].startswith(f"{stack_name}-APIRESOLVE")
            and "TestRunnerFunction" in f["FunctionName"]
        ),
        None,
    )

    if not test_runner_function:
        raise ValueError(f"TestRunnerFunction not found for stack {stack_name}")

    # Prepare payload
    payload = {
        "arguments": {
            "input": {
                "testSetId": test_set,
            }
        }
    }

    # Add context if provided
    if context:
        payload["arguments"]["input"]["context"] = context

    # Add numberOfFiles if provided
    if number_of_files is not None:
        payload["arguments"]["input"]["numberOfFiles"] = number_of_files

    # Add configVersion if provided
    if config_version:
        payload["arguments"]["input"]["configVersion"] = config_version
    # Pin the revision too, or the run records a profile while actually using
    # whatever that profile currently holds.
    if config_revision is not None:
        payload["arguments"]["input"]["configRevision"] = int(config_revision)

    console.print(f"[bold blue]Starting test run for test set: {test_set}[/bold blue]")
    if number_of_files:
        console.print(f"[blue]Limiting to {number_of_files} files[/blue]")

    # Invoke test runner lambda
    response = lambda_client.invoke(
        FunctionName=test_runner_function, Payload=json.dumps(payload)
    )

    # Parse response
    result = json.loads(response["Payload"].read())

    # Check for Lambda function errors (AWS returns StatusCode=200 for invocation success,
    # but function errors are indicated by errorMessage in the payload)
    if "errorMessage" in result:
        error_type = result.get("errorType", "Unknown")
        error_msg = result["errorMessage"]
        raise ValueError(f"Test runner execution failed ({error_type}): {error_msg}")

    if response["StatusCode"] != 200:
        raise ValueError(
            f"Test runner invocation failed with status {response['StatusCode']}"
        )

    console.print(f"[green]✓ Test run started: {result['testRunId']}[/green]")
    return result


def _get_test_set_document_ids(
    stack_name: str,
    test_set: str,
    batch_id: str,
    region: Optional[str],
    resources: dict,
):
    """Get document IDs from test set for monitoring"""

    # Get test set bucket from resources
    test_set_bucket = resources.get("TestSetBucket")
    if not test_set_bucket:
        raise ValueError("TestSetBucket not found in stack resources")

    # List files in test set input directory
    s3_client = boto3.client("s3", region_name=region)

    try:
        response = s3_client.list_objects_v2(
            Bucket=test_set_bucket, Prefix=f"{test_set}/input/"
        )

        document_ids = []
        if "Contents" in response:
            for obj in response["Contents"]:
                key = obj["Key"]
                if key.endswith("/"):  # Skip directories
                    continue
                # Create document_id as batch_id/filename
                filename = key.split("/")[-1]
                doc_id = f"{batch_id}/{filename}"
                document_ids.append(doc_id)

        return document_ids

    except Exception as e:
        console.print(
            f"[yellow]Warning: Could not get document IDs from test set: {e}[/yellow]"
        )
        return []  # Return empty list if we can't get IDs


def _manifest_has_baselines(manifest_path: str) -> bool:
    """Check if manifest has baseline_source column populated"""
    import pandas as pd

    try:
        if manifest_path.endswith(".json"):
            df = pd.read_json(manifest_path)
        else:
            df = pd.read_csv(manifest_path)

        return "baseline_source" in df.columns and df["baseline_source"].notna().any()
    except Exception:
        return False


def _create_test_set_from_manifest(
    manifest_path: str,
    test_set_name: str,
    stack_name: str,
    region: Optional[str],
    resources: dict,
):
    """Create test set structure from manifest files"""
    import os

    import pandas as pd

    # Get test set bucket
    test_set_bucket = resources.get("TestSetBucket")
    if not test_set_bucket:
        raise ValueError("TestSetBucket not found in stack resources")

    s3_client = boto3.client("s3", region_name=region)

    # Read manifest
    if manifest_path.endswith(".json"):
        df = pd.read_json(manifest_path)
    else:
        df = pd.read_csv(manifest_path)

    console.print(
        f"[bold blue]Creating test set '{test_set_name}' from manifest...[/bold blue]"
    )

    # Clear existing test set folder if it exists
    try:
        response = s3_client.list_objects_v2(
            Bucket=test_set_bucket, Prefix=f"{test_set_name}/"
        )

        if "Contents" in response:
            # Delete all existing objects in the test set folder
            objects_to_delete = [{"Key": obj["Key"]} for obj in response["Contents"]]

            if objects_to_delete:
                s3_client.delete_objects(
                    Bucket=test_set_bucket,
                    Delete={"Objects": objects_to_delete},
                )
                console.print("  Cleared existing test set files")

    except Exception as e:
        console.print(f"[yellow]Warning: Could not clear existing files: {e}[/yellow]")

    # Place .uploading marker to prevent resolver race condition (issue #193)
    marker_key = f"{test_set_name}/.uploading"
    s3_client.put_object(
        Bucket=test_set_bucket, Key=marker_key, Body=b"upload-in-progress"
    )

    # Copy input files
    for _, row in df.iterrows():
        source_path = row["document_path"]
        filename = os.path.basename(source_path)

        # Upload to test set input directory
        s3_key = f"{test_set_name}/input/{filename}"

        if source_path.startswith("s3://"):
            # Copy from S3 to S3
            source_bucket, source_key = source_path[5:].split("/", 1)
            s3_client.copy_object(
                CopySource={"Bucket": source_bucket, "Key": source_key},
                Bucket=test_set_bucket,
                Key=s3_key,
            )
        else:
            # Upload from local file
            s3_client.upload_file(source_path, test_set_bucket, s3_key)

        # Copy baseline if exists
        if "baseline_source" in row and pd.notna(row["baseline_source"]):
            baseline_path = row["baseline_source"]

            # Upload all files in the baseline directory recursively
            import glob as glob_module

            baseline_files = glob_module.glob(
                os.path.join(baseline_path, "**", "*"), recursive=True
            )
            for baseline_file in baseline_files:
                if os.path.isfile(baseline_file):
                    # Preserve directory structure relative to baseline_path
                    rel_path = os.path.relpath(baseline_file, baseline_path)
                    s3_key = f"{test_set_name}/baseline/{filename}/{rel_path}"
                    s3_client.upload_file(baseline_file, test_set_bucket, s3_key)

    # Remove .uploading marker now that all files are uploaded (issue #193)
    try:
        s3_client.delete_object(Bucket=test_set_bucket, Key=marker_key)
    except Exception as e:
        console.print(f"[yellow]Warning: Could not remove upload marker: {e}[/yellow]")

    console.print(
        f"[green]✓ Test set '{test_set_name}' created with {len(df)} files[/green]"
    )


@cli.command(name="stop-workflows")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--skip-purge",
    is_flag=True,
    help="Skip purging the SQS queue",
)
@click.option(
    "--skip-stop",
    is_flag=True,
    help="Skip stopping Step Function executions",
)
@click.option("--region", help="AWS region (optional)")
def stop_workflows(
    stack_name: str,
    skip_purge: bool,
    skip_stop: bool,
    region: Optional[str],
):
    """
    Stop all running workflows for a stack

    This command purges the SQS document queue and stops all running
    Step Function executions. Use this to halt processing when needed.

    Examples:

      # Stop all workflows (purge queue + stop executions)
      idp-cli stop-workflows --stack-name my-stack

      # Only purge the queue
      idp-cli stop-workflows --stack-name my-stack --skip-stop

      # Only stop executions (don't purge queue)
      idp-cli stop-workflows --stack-name my-stack --skip-purge
    """
    try:
        from idp_sdk import IDPClient

        console.print(
            f"[bold blue]Stopping workflows for stack: {stack_name}[/bold blue]"
        )
        console.print()

        client = IDPClient(stack_name=stack_name, region=region)
        result = client.batch.stop_workflows(skip_purge=skip_purge, skip_stop=skip_stop)

        # Show executions stopped result
        if result.executions_stopped:
            exec_result = result.executions_stopped
            if exec_result.error:
                console.print(f"[red]✗ Failed: {exec_result.error}[/red]")
                sys.exit(1)

            console.print(
                f"\n[green]✓ Stopped {exec_result.total_stopped} executions[/green]"
            )
            if exec_result.total_failed > 0:
                console.print(
                    f"[yellow]  {exec_result.total_failed} failed to stop[/yellow]"
                )

            # Show verification result
            if exec_result.remaining > 0:
                console.print(
                    f"[red]⚠ Warning: {exec_result.remaining} executions still running[/red]"
                )
                console.print(
                    "[yellow]  New executions may have started during stop operation[/yellow]"
                )
                console.print(
                    "[yellow]  Run command again to stop remaining executions[/yellow]"
                )
            else:
                console.print(
                    "[green]✓ Verified: No running executions remaining[/green]"
                )

        # Show documents aborted result
        if result.documents_aborted:
            abort_result = result.documents_aborted
            if abort_result.error:
                console.print(
                    f"[yellow]⚠ Could not abort queued documents: {abort_result.error}[/yellow]"
                )
            elif abort_result.documents_aborted > 0:
                console.print(
                    f"\n[green]✓ Updated {abort_result.documents_aborted} queued documents to ABORTED status[/green]"
                )

    except Exception as e:
        logger.error(f"Error stopping workflows: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="load-test")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--source-file",
    required=True,
    type=str,
    help="Source file to copy (local path or s3://bucket/key)",
)
@click.option(
    "--rate",
    default=100,
    type=int,
    help="Files per minute (default: 100)",
)
@click.option(
    "--duration",
    default=1,
    type=int,
    help="Duration in minutes (default: 1)",
)
@click.option(
    "--schedule",
    type=click.Path(exists=True),
    help="CSV schedule file (minute,count) - overrides --rate and --duration",
)
@click.option(
    "--dest-prefix",
    default="load-test",
    help="Destination prefix in input bucket (default: load-test)",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    help="Configuration profile to use for processing (default: active profile); --config-version is the former name and still works",
)
@click.option("--region", help="AWS region (optional)")
def load_test(
    stack_name: str,
    source_file: str,
    rate: int,
    duration: int,
    schedule: Optional[str],
    dest_prefix: str,
    config_version: Optional[str],
    region: Optional[str],
):
    """
    Run load test by copying files to input bucket

    Use this to test system performance under load. The source file is copied
    multiple times to the input bucket, triggering document processing.

    Examples:

      # Constant rate: 100 files/minute for 5 minutes
      idp-cli load-test --stack-name my-stack --source-file samples/invoice.pdf --rate 100 --duration 5

      # High volume: 2500 files/minute for 1 minute
      idp-cli load-test --stack-name my-stack --source-file samples/invoice.pdf --rate 2500

      # Use schedule file for variable rates
      idp-cli load-test --stack-name my-stack --source-file samples/invoice.pdf --schedule schedule.csv

      # Use S3 source file
      idp-cli load-test --stack-name my-stack --source-file s3://my-bucket/test.pdf --rate 500

      # Load test with a specific config profile
      idp-cli load-test --stack-name my-stack --source-file samples/invoice.pdf --rate 100 --config-profile v2

    Schedule file format (CSV):
      minute,count
      1,100
      2,200
      3,500
    """
    try:
        _client = IDPClient(stack_name=stack_name, region=region)
        result = _client.testing.load_test(
            source_file=source_file,
            stack_name=stack_name,
            rate=rate,
            duration=duration,
            schedule_file=schedule,
            dest_prefix=dest_prefix,
            config_version=config_version,
        )

        if not result.success:
            console.print(f"[red]✗ Load test failed: {result.error}[/red]")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error running load test: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="remove-deleted-stack-resources")
@click.option(
    "--region",
    default="us-west-2",
    help="Primary AWS region for regional resources like log groups (default: us-west-2)",
)
@click.option("--profile", help="AWS profile to use")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview changes without making them (RECOMMENDED first step)",
)
@click.option(
    "--yes",
    "-y",
    "auto_approve",
    is_flag=True,
    help="Auto-approve all deletions (skip confirmations)",
)
@click.option(
    "--check-stack-regions",
    default="us-east-1,us-west-2,eu-central-1",
    help="Comma-separated list of regions to check for IDP stacks (default: us-east-1,us-west-2,eu-central-1)",
)
def remove_residual_resources_from_deleted_stacks(
    region: str,
    profile: Optional[str],
    dry_run: bool,
    auto_approve: bool,
    check_stack_regions: str,
):
    """
    Remove residual AWS resources left behind from deleted IDP stacks

    ⚠️  CAUTION: This command permanently deletes AWS resources.
    Always run with --dry-run first to review what will be deleted.

    WHAT THIS COMMAND DOES:
    When IDP CloudFormation stacks are deleted, some resources may remain
    (CloudFront distributions, IAM policies, log groups, etc.). This command
    safely identifies and removes ONLY those residual resources.

    HOW IT IDENTIFIES IDP RESOURCES:
    1. Scans CloudFormation in multiple regions for IDP stacks
    2. Identifies IDP stacks by their Description ("AWS GenAI IDP Accelerator")
       or naming patterns (IDP-*, PATTERN1/2/3, etc.)
    3. Tracks both ACTIVE stacks (protected) and DELETED stacks (cleanup targets)
    4. Only targets resources that belong to stacks in DELETE_COMPLETE state
    5. Resources from active stacks are NEVER touched

    SAFETY FEATURES:
    - Multi-region stack discovery (customizable with --check-stack-regions)
    - Resources from ACTIVE stacks are protected and skipped
    - Resources from UNKNOWN stacks (not verified as IDP) are skipped
    - Interactive confirmation for each resource (unless --yes)
    - Options: y=yes, n=no, a=yes to all of type, s=skip all of type
    - --dry-run mode shows exactly what would be deleted

    RESOURCES CLEANED UP:
    - CloudFront distributions
    - CloudFront response header policies
    - CloudWatch log groups
    - AppSync APIs
    - IAM policies
    - CloudWatch Logs resource policy entries

    CLOUDFRONT TWO-PHASE CLEANUP:
    CloudFront requires distributions to be disabled before deletion:
    1. First run: Disables orphaned distributions
    2. Wait 15-20 minutes for CloudFront propagation
    3. Second run: Deletes the disabled distributions

    Examples:

      # RECOMMENDED: Always dry-run first
      idp-cli remove-deleted-stack-resources --dry-run

      # Interactive cleanup with confirmations
      idp-cli remove-deleted-stack-resources

      # Use specific AWS profile
      idp-cli remove-deleted-stack-resources --profile my-profile

      # Auto-approve all deletions (USE WITH CAUTION)
      idp-cli remove-deleted-stack-resources --yes

      # Check additional regions for stacks
      idp-cli remove-deleted-stack-resources --check-stack-regions us-east-1,us-west-2,eu-central-1,eu-west-1
    """
    try:
        # Parse regions list
        regions_list = [r.strip() for r in check_stack_regions.split(",")]

        client = IDPClient(region=region)
        cleanup_result = client.stack.cleanup_orphaned(
            dry_run=dry_run,
            auto_approve=auto_approve,
            regions=regions_list,
            profile=profile,
        )
        results = cleanup_result.results

        # Print summary
        console.print()
        console.print("[bold]CLEANUP SUMMARY[/bold]")
        console.print("=" * 60)

        has_errors = False
        has_disabled = False

        for resource_type, result in results.items():
            resource_name = resource_type.upper().replace("_", " ")
            console.print(f"\n[bold]{resource_name}:[/bold]")

            if result.get("deleted"):
                console.print(f"  [green]Deleted ({len(result['deleted'])}):[/green]")
                for item in result["deleted"]:
                    console.print(f"    - {item}")

            if result.get("disabled"):
                has_disabled = True
                console.print(
                    f"  [yellow]Disabled ({len(result['disabled'])}):[/yellow]"
                )
                for item in result["disabled"]:
                    console.print(f"    - {item}")

            if result.get("updated"):
                console.print(f"  [cyan]Updated ({len(result['updated'])}):[/cyan]")
                for item in result["updated"]:
                    console.print(f"    - {item}")

            if result.get("errors"):
                has_errors = True
                console.print(f"  [red]Errors ({len(result['errors'])}):[/red]")
                for error in result["errors"]:
                    console.print(f"    - {error}")

            if not any(
                result.get(key) for key in ["deleted", "disabled", "updated", "errors"]
            ):
                console.print("  No resources found")

        # Show next steps if CloudFront distributions were disabled
        if has_disabled:
            console.print()
            console.print("[bold yellow]NEXT STEPS[/bold yellow]")
            console.print("=" * 60)
            console.print(
                "CloudFront distributions have been disabled and are deploying."
            )
            console.print("Wait 15-20 minutes, then re-run this command to:")
            console.print("  • Delete the disabled distributions")
            console.print("  • Retry failed policy deletions")
            console.print()
            console.print("Re-run command:")
            console.print(
                f"  [cyan]idp-cli remove-deleted-stack-resources --region {region}[/cyan]"
            )
            console.print("=" * 60)

        if has_errors:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error cleaning up orphaned resources: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="config-create")
@click.option(
    "--features",
    default="min",
    help="Feature set: 'min' (classification, extraction, classes), 'core' (adds ocr, assessment), 'all', or comma-separated list of sections",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path (default: stdout)",
)
@click.option(
    "--include-prompts",
    is_flag=True,
    help="Include full prompt templates (default: stripped for readability)",
)
@click.option(
    "--no-comments",
    is_flag=True,
    help="Omit explanatory header comments",
)
def config_create(
    features: str,
    output: Optional[str],
    include_prompts: bool,
    no_comments: bool,
):
    """
    Generate an IDP configuration template

    Creates a YAML configuration file based on system defaults. Users only need
    to customize the values they want to change - unspecified fields use defaults.

    Feature sets:
      min:  classification, extraction, classes (simplest)
      core: min + ocr, assessment
      all:  all sections with full defaults

    Or specify a comma-separated list of sections:
      --features "classification,extraction,summarization"

    Examples:

      # Generate minimal config to stdout
      idp-cli config-create

      # Generate full config with all sections
      idp-cli config-create --features all --output full-config.yaml

      # Include full prompts (verbose)
      idp-cli config-create --features core --include-prompts --output config.yaml

      # Custom section selection
      idp-cli config-create --features "classification,extraction,summarization" --output config.yaml
    """
    try:
        from idp_common.config.merge_utils import generate_config_template

        # Parse features - could be a preset or comma-separated list
        if "," in features:
            feature_list = [f.strip() for f in features.split(",")]
        else:
            feature_list = features  # type: ignore

        # Generate template
        yaml_content = generate_config_template(
            features=feature_list,
            pattern="pattern-2",
            include_prompts=include_prompts,
            include_comments=not no_comments,
        )

        if output:
            # Write to file
            with open(output, "w", encoding="utf-8") as f:
                f.write(yaml_content)
            console.print(
                f"[green]✓ Configuration template written to: {output}[/green]"
            )
            console.print()
            console.print("[bold]Next steps:[/bold]")
            console.print(f"  1. Edit {output} to add your document classes")
            console.print(
                f"  2. Validate: [cyan]idp-cli config-validate --config-file {output}[/cyan]"
            )
            console.print(
                f"  3. Deploy: [cyan]idp-cli deploy --stack-name <name> --custom-config {output}[/cyan]"
            )
        else:
            # Write to stdout
            console.print(yaml_content)

    except FileNotFoundError as e:
        console.print(f"[red]✗ Error: {e}[/red]")
        console.print(
            "[yellow]Tip: Run from the project root directory or set IDP_PROJECT_ROOT[/yellow]"
        )
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error creating config: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="config-validate")
@click.option(
    "--config-file",
    "-f",
    required=True,
    type=click.Path(exists=True),
    help="Path to configuration file to validate",
)
@click.option(
    "--show-merged",
    is_flag=True,
    help="Show the full merged configuration",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Fail validation if config contains unknown or deprecated fields",
)
@click.option(
    "--emit-migrated",
    type=click.Path(),
    default=None,
    help=(
        "Write the config migrated to the current format (e.g. v0.5 -> v0.6) to this "
        "path. Useful for pre-migrating a saved older config file before import."
    ),
)
def config_validate(
    config_file: str,
    show_merged: bool,
    strict: bool,
    emit_migrated: Optional[str],
):
    """
    Validate a configuration file against system defaults

    Checks that the configuration:
      - Has valid YAML syntax
      - Merges correctly with system defaults
      - Passes Pydantic model validation
      - Has valid model IDs and settings

    Examples:

      # Validate a config file
      idp-cli config-validate --config-file ./my-config.yaml

      # Show the full merged config
      idp-cli config-validate --config-file ./config.yaml --show-merged

      # Strict mode - fail if unknown/deprecated fields are present
      idp-cli config-validate --config-file ./config.yaml --strict
    """
    try:
        import yaml

        from idp_common.config.merge_utils import load_yaml_file, validate_config

        # Load the user's config
        console.print(f"[bold blue]Validating: {config_file}[/bold blue]")
        console.print()

        try:
            from pathlib import Path

            user_config = load_yaml_file(Path(config_file))
            console.print("[green]✓ YAML syntax valid[/green]")
        except yaml.YAMLError as e:
            console.print(f"[red]✗ YAML syntax error: {e}[/red]")
            sys.exit(1)
        except Exception as e:
            console.print(f"[red]✗ Failed to load file: {e}[/red]")
            sys.exit(1)

        # Emit the migrated (current-format) config if requested. Done up front so it
        # works even when the file is an older format that would otherwise warn about
        # deprecated fields — the whole point is to hand the user a pre-migrated file.
        if emit_migrated:
            from idp_common.config.migrations import migrate_config

            migrated = migrate_config(user_config)
            migrated_yaml = yaml.dump(
                migrated,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
                width=120,
            )
            with open(emit_migrated, "w", encoding="utf-8") as f:
                f.write(
                    f"# Migrated to config_format_version {migrated.get('config_format_version', '?')} "
                    f"from: {config_file}\n\n"
                )
                f.write(migrated_yaml)
            console.print(
                f"[green]✓ Migrated config written to: {emit_migrated}[/green]"
            )

        # Check for extra/deprecated fields before Pydantic validation
        from idp_common.config.models import IDP_CONFIG_DEPRECATED_FIELDS, IDPConfig

        defined_fields = set(IDPConfig.model_fields.keys())
        user_fields = set(user_config.keys())
        extra_fields = user_fields - defined_fields

        deprecated_fields = extra_fields & IDP_CONFIG_DEPRECATED_FIELDS
        unknown_fields = extra_fields - IDP_CONFIG_DEPRECATED_FIELDS

        if deprecated_fields:
            console.print(
                f"[yellow]⚠ Deprecated fields found (will be ignored): {sorted(deprecated_fields)}[/yellow]"
            )

        if unknown_fields:
            console.print(
                f"[yellow]⚠ Unknown fields found (will be ignored): {sorted(unknown_fields)}[/yellow]"
            )

        if strict and extra_fields:
            console.print()
            console.print("[red]✗ Strict mode: config contains extra fields[/red]")
            console.print(
                "[yellow]Remove these fields or run without --strict[/yellow]"
            )
            sys.exit(1)

        # Validate config
        result = validate_config(user_config, pattern="pattern-2")

        if result["valid"]:
            console.print("[green]✓ Config merges with system defaults[/green]")
            console.print("[green]✓ Pydantic validation passed[/green]")

            # Show warnings
            if result["warnings"]:
                console.print()
                console.print("[bold yellow]Warnings:[/bold yellow]")
                for warning in result["warnings"]:
                    console.print(f"  ⚠ {warning}")

            # Check for document classes
            classes = user_config.get("classes", [])
            if classes:
                console.print(
                    f"[green]✓ {len(classes)} document class(es) defined[/green]"
                )
            else:
                console.print(
                    "[yellow]⚠ No document classes defined - add at least one[/yellow]"
                )

            console.print()
            console.print("[bold green]Config is valid![/bold green]")

            if show_merged:
                console.print()
                console.print("[bold]Merged configuration:[/bold]")
                console.print("-" * 60)
                merged_yaml = yaml.dump(
                    result["merged_config"],
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
                console.print(merged_yaml)

        else:
            console.print("[red]✗ Validation failed[/red]")
            console.print()
            for error in result["errors"]:
                console.print(f"  [red]• {error}[/red]")
            sys.exit(1)

    except FileNotFoundError as e:
        console.print(f"[red]✗ Error: {e}[/red]")
        console.print(
            "[yellow]Tip: Run from the project root directory or set IDP_PROJECT_ROOT[/yellow]"
        )
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error validating config: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="config-upload")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--config-file",
    "-f",
    required=True,
    type=click.Path(exists=True),
    help="Path to configuration file (YAML or JSON)",
)
@click.option(
    "--validate/--no-validate",
    default=True,
    help="Validate config before uploading (default: validate)",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    required=True,
    help="Configuration profile to update (e.g., v1, v2). If the profile doesn't exist, it will be created. --config-version is the former name and still works.",
)
@click.option(
    "--version-description",
    help="Description for the configuration profile (used when creating new profiles)",
)
@click.option(
    "--revision-notes",
    help=(
        "What this upload changed, recorded on the revision it cuts and shown as "
        "Notes in the revision history (e.g. 'raised topK to 20'). Distinct from "
        "--version-description, which sets the profile's description and is "
        "overwritten by every save."
    ),
)
@click.option("--region", help="AWS region (optional)")
def config_upload(
    stack_name: str,
    config_file: str,
    validate: bool,
    config_version: Optional[str],
    version_description: Optional[str],
    revision_notes: Optional[str],
    region: Optional[str],
):
    """
    Upload a configuration file to a deployed IDP stack

    Reads a local YAML or JSON configuration file and uploads it to the
    stack's ConfigurationTable in DynamoDB. The config is merged with
    system defaults just like configurations saved through the Web UI.

    Supports configuration profiles - can update existing profiles or
    create new profiles with optional descriptions.

    Examples:

      # Upload config with validation (updates the active profile)
      idp-cli config-upload --stack-name my-stack --config-file ./config.yaml

      # Update an existing profile
      idp-cli config-upload --stack-name my-stack --config-file ./config.yaml --config-profile Production

      # Create a new profile with a description
      idp-cli config-upload --stack-name my-stack --config-file ./config.yaml --config-profile NewProfile --version-description "Test configuration for new feature"

      # Skip validation (use with caution)
      idp-cli config-upload --stack-name my-stack --config-file ./config.yaml --no-validate
    """
    try:
        from idp_sdk import IDPClient

        console.print(f"[bold blue]Uploading config to stack: {stack_name}[/bold blue]")
        console.print(f"Config file: {config_file}")
        console.print()

        client = IDPClient(stack_name=stack_name, region=region)

        # Warn for the default profile
        if config_version and config_version.lower() == "default":
            console.print(
                "[yellow]⚠️  Warning: This will update the default [system default] config profile[/yellow]"
            )

        result = client.config.upload(
            config_file=config_file,
            validate=validate,
            config_version=config_version,
            description=version_description,
            revision_notes=revision_notes,
        )

        if not result.success:
            console.print(
                f"[red]✗ Failed to upload configuration: {result.error}[/red]"
            )
            if result.error and "Validation" in result.error:
                console.print(
                    "[yellow]Use --no-validate to skip validation (not recommended)[/yellow]"
                )
            sys.exit(1)

        console.print("[green]✓ Configuration uploaded successfully[/green]")
        console.print()
        if config_version:
            action = "created" if result.version_created else "updated"
            console.print(
                f"[bold]Configuration profile '{config_version}' {action}![/bold]"
            )
            if result.revision is not None:
                # Printed so a script can pin the run to exactly what it just
                # uploaded instead of naming a new profile per iteration.
                console.print(f"Revision: r{result.revision}")
                console.print(
                    f"Process under it with: --config-profile {config_version} "
                    f"--config-revision {result.revision}"
                )
            else:
                console.print(
                    "Use --config-profile to process documents with this profile."
                )
        else:
            console.print("[bold]Configuration is now active![/bold]")
            console.print("New documents will use this configuration immediately.")

    except Exception as e:
        logger.error(f"Error uploading config: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="config-download")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path (default: stdout)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["full", "minimal"]),
    default="full",
    help="Output format: 'full' (complete config) or 'minimal' (only differences from defaults)",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    help="Configuration profile to download (e.g., v1, v2). If not specified, downloads the active profile. --config-version is the former name and still works.",
)
@click.option(
    "--config-revision",
    type=int,
    help=(
        "Download an exact revision of the profile (e.g. 7) instead of its "
        "current configuration. Requires --config-profile."
    ),
)
@click.option("--region", help="AWS region (optional)")
def config_download(
    stack_name: str,
    output: Optional[str],
    output_format: str,
    config_version: Optional[str],
    config_revision: Optional[int],
    region: Optional[str],
):
    """
    Download configuration from a deployed IDP stack

    Retrieves the current configuration from DynamoDB and optionally
    shows only the values that differ from system defaults.

    Examples:

      # Download full config
      idp-cli config-download --stack-name my-stack --output config.yaml

      # Download minimal config (only customizations)
      idp-cli config-download --stack-name my-stack --format minimal --output config.yaml

      # Print to stdout
      idp-cli config-download --stack-name my-stack

      # Download an exact revision of a profile (what an earlier run used)
      idp-cli config-download --stack-name my-stack --config-profile lending \\
          --config-revision 7 --output r7.yaml
    """
    try:
        from idp_sdk import IDPClient

        if config_revision is not None and not config_version:
            # Revisions belong to a profile; "revision 7 of whatever is active"
            # would silently change meaning the moment someone activates
            # another profile.
            console.print(
                "[red]Error: --config-revision requires --config-profile[/red]"
            )
            sys.exit(1)

        console.print(
            f"[bold blue]Downloading config from stack: {stack_name}[/bold blue]"
        )
        if config_revision is not None:
            console.print(f"Profile: {config_version} (revision r{config_revision})")

        client = IDPClient(stack_name=stack_name, region=region)
        result = client.config.download(
            format=output_format,
            config_version=config_version,
            config_revision=config_revision,
            output=output,
        )

        if output:
            console.print(f"[green]✓ Configuration saved to: {output}[/green]")
        else:
            console.print()
            console.print(result.yaml_content)

    except Exception as e:
        logger.error(f"Error downloading config: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="config-activate")
@click.option(
    "--stack-name",
    required=True,
    help="CloudFormation stack name",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    required=True,
    help="Configuration profile to activate; --config-version is the former name and still works",
)
@click.option("--region", help="AWS region (optional)")
def config_activate(
    stack_name: str,
    config_version: str,
    region: str = None,
):
    """
    Activate a configuration profile in a deployed IDP stack

    Sets the specified configuration profile as the active one.
    All new document processing will use this configuration.

    If the configuration has use_bda enabled, it will automatically sync
    to BDA before activation (matching UI behavior).

    Examples:
      # Activate a specific profile
      idp-cli config-activate --stack-name my-stack --config-profile v2

      # Activate the default profile
      idp-cli config-activate --stack-name my-stack --config-profile default
    """
    try:
        from idp_sdk import IDPClient

        console.print(
            f"[bold blue]Activating config profile in stack: {stack_name}[/bold blue]"
        )
        console.print(f"Version: {config_version}")
        console.print()

        client = IDPClient(stack_name=stack_name, region=region)
        result = client.config.activate(config_version=config_version)

        if not result.success:
            console.print(
                f"[red]✗ Failed to activate configuration profile '{config_version}': {result.error}[/red]"
            )
            if result.error and "does not exist" in result.error:
                console.print(
                    f"Use 'idp-cli config-list --stack-name {stack_name}' to see available versions"
                )
            sys.exit(1)

        # Show BDA sync results if performed
        if result.bda_synced:
            if result.bda_classes_failed > 0:
                console.print(
                    f"[yellow]⚠ BDA sync partial: {result.bda_classes_synced} succeeded, "
                    f"{result.bda_classes_failed} failed[/yellow]"
                )
            else:
                console.print(
                    f"[green]✓ Successfully synced {result.bda_classes_synced} classes to BDA[/green]"
                )

        console.print(
            f"[green]✓ Successfully activated configuration profile: {config_version}[/green]"
        )
        console.print("New documents will use this configuration immediately.")

    except Exception as e:
        logger.error(f"Error activating config: {e}", exc_info=True)
        console.print(f"[red]✗ Failed to activate configuration: {e}[/red]")
        sys.exit(1)


@cli.command(name="config-list")
@click.option(
    "--stack-name",
    required=True,
    help="CloudFormation stack name",
)
@click.option("--region", help="AWS region (optional)")
def config_list(stack_name: str, region: str = None):
    """
    List all configuration profiles in a deployed IDP stack

    Shows all available configuration profiles with their status,
    creation dates, and descriptions.

    Examples:
      # List all configuration profiles
      idp-cli config-list --stack-name my-stack
    """
    try:
        from idp_sdk import IDPClient

        console.print(
            f"[bold blue]Listing configuration profiles in stack: {stack_name}[/bold blue]"
        )

        client = IDPClient(stack_name=stack_name, region=region)
        result = client.config.list()

        if not result.versions:
            console.print("[yellow]No configuration profiles found[/yellow]")
            return

        console.print(
            f"\n[bold]Found {result.count} configuration profile(s):[/bold]\n"
        )

        table = Table(show_header=True, header_style="bold blue")
        table.add_column("Profile Name", style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("Rev", justify="right")
        table.add_column("Created", style="dim")
        table.add_column("Updated", style="dim")
        table.add_column("Description", style="green")

        for version in sorted(result.versions, key=lambda v: v.version_name):
            status = "[bold green]ACTIVE[/bold green]" if version.is_active else ""
            created = (
                version.created_at.replace("T", " ").replace("Z", "")
                if version.created_at
                else ""
            )
            updated = (
                version.updated_at.replace("T", " ").replace("Z", "")
                if version.updated_at
                else ""
            )
            # The current revision, so `config-list` alone tells a script what to
            # pin. A profile with no history shows nothing rather than "r0".
            current = (
                version.published_revision
                if version.published_revision is not None
                else version.latest_revision
            )
            table.add_row(
                version.version_name,
                status,
                f"r{current}" if current is not None else "",
                created,
                updated,
                version.description or "",
            )

        console.print(table)
        console.print(
            "\n[dim]Rev = the revision each profile's current configuration "
            "reflects. See revision history with: idp-cli config-revisions "
            "--config-profile <name>[/dim]"
        )

    except Exception as e:
        logger.error(f"Error listing configs: {e}", exc_info=True)
        console.print(f"[red]✗ Failed to list configurations: {e}[/red]")
        sys.exit(1)


@cli.command(name="config-revisions")
@click.option(
    "--stack-name",
    required=True,
    help="CloudFormation stack name",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    required=True,
    help="Configuration profile whose revision history to list; --config-version is the former name and still works",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit JSON instead of a table (for scripting)",
)
@click.option("--region", help="AWS region (optional)")
def config_revisions(
    stack_name: str,
    config_version: str,
    as_json: bool,
    region: Optional[str] = None,
):
    """
    List the revision history of a Configuration Profile

    Every save of a profile cuts an immutable revision. This shows the ones still
    retained: the last 20, plus anything labeled, pinned by a test run, or
    currently in use.

    Pair it with `config-download --config-revision` to fetch an earlier
    configuration, and with `--config-revision` on `process` / `run-inference` to
    process under an exact revision. Together those let an automated tuning loop
    keep ONE profile and track its iterations as revisions, instead of minting a
    new named profile per iteration.

    Examples:

      # Revision history of a profile
      idp-cli config-revisions --stack-name my-stack --config-profile lending

      # Machine-readable, e.g. to pick the newest revision
      idp-cli config-revisions --stack-name my-stack --config-profile lending --json
    """
    try:
        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)
        result = client.config.revisions(config_profile=config_version)

        if as_json:
            console.print_json(result.model_dump_json())
            return

        console.print(
            f"[bold blue]Revision history of configuration profile "
            f"'{result.profile}' in stack: {stack_name}[/bold blue]"
        )

        if not result.revisions:
            # Distinguish "no history" from "profile does not exist" — the first is
            # normal on a profile untouched since the stack was upgraded.
            console.print(
                "\n[yellow]No revisions recorded for this profile.[/yellow] "
                "A profile has no history until it is saved on a release that "
                "records revisions."
            )
            return

        console.print(f"\n[bold]{result.count} revision(s) retained:[/bold]\n")

        table = Table(show_header=True, header_style="bold blue")
        table.add_column("Rev", justify="right", style="cyan")
        table.add_column("Current", justify="center")
        table.add_column("Created", style="dim")
        table.add_column("By")
        table.add_column("Label")
        table.add_column("Notes", style="green")
        table.add_column("Keep", justify="center")

        for revision in result.revisions:
            created = (
                revision.created_at.replace("T", " ").replace("Z", "")
                if revision.created_at
                else ""
            )
            # "Keep" is why a revision survives pruning; without it a user cannot
            # tell which of their revisions the retention cap will drop next.
            keep = []
            if revision.label:
                keep.append("labeled")
            if revision.pinned:
                keep.append("test run")
            table.add_row(
                f"r{revision.revision}",
                "[bold green]←[/bold green]" if revision.published else "",
                created,
                revision.created_by or "",
                revision.label or "",
                revision.notes or "",
                ", ".join(keep),
            )

        console.print(table)
        console.print(
            f"\n[dim]Download one: idp-cli config-download --stack-name {stack_name} "
            f"--config-profile {result.profile} --config-revision <n>[/dim]"
        )

    except Exception as e:
        logger.error(f"Error listing config revisions: {e}", exc_info=True)
        console.print(f"[red]✗ Failed to list revisions: {e}[/red]")
        sys.exit(1)


def _profile_is_managed(client, profile: str) -> bool:
    """
    Whether `profile` is stack-managed, best effort.

    Only consulted on the interactive path, so the extra listing costs nothing in
    scripts. A failure here must never block a delete — the answer is advisory.
    """
    try:
        return any(
            v.version_name == profile and v.managed
            for v in client.config.list().versions
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Could not determine whether {profile} is managed: {e}")
        return False


@cli.command(name="config-delete")
@click.option(
    "--stack-name",
    required=True,
    help="CloudFormation stack name",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    required=True,
    help="Configuration profile to delete; --config-version is the former name and still works",
)
@click.option(
    "--force",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.option("--region", help="AWS region (optional)")
def config_delete(
    stack_name: str,
    config_version: str,
    force: bool,
    region: str = None,
):
    """
    Delete a configuration profile from a deployed IDP stack

    Removes the specified configuration profile, and its revision history, from
    the stack. Cannot delete the 'default' profile or the currently active one.

    Unlike the Web UI, this can delete a **stack-managed** profile. That is
    deliberate: an extension that installed profiles and was then uninstalled can
    leave managed profiles behind, and the UI refuses them because normally the
    owning stack would recreate them. You are warned before confirming.

    Examples:
      # Delete a profile with confirmation
      idp-cli config-delete --stack-name my-stack --config-profile old-profile

      # Delete without confirmation prompt
      idp-cli config-delete --stack-name my-stack --config-profile old-profile --force
    """
    try:
        from idp_sdk import IDPClient

        console.print(
            f"[bold blue]Deleting config profile from stack: {stack_name}[/bold blue]"
        )
        console.print(f"Profile: {config_version}")

        client = IDPClient(stack_name=stack_name, region=region)

        # Confirmation prompt
        if not force:
            # A stack-managed profile is the interesting case: the Web UI refuses to
            # delete one, so anyone here is probably cleaning up after an extension
            # that installed profiles and left them behind. Say which it is rather
            # than deleting a built-in preset the next stack update would restore.
            if _profile_is_managed(client, config_version):
                console.print(
                    f"[yellow]⚠ '{config_version}' is a stack-managed profile.[/yellow] "
                    "If its owning stack (an extension, or this stack's built-in "
                    "presets) is still installed, it will be recreated on the next "
                    "update. Deleting is the right move only when that stack is gone."
                )
            if not click.confirm(
                f"Are you sure you want to delete configuration profile '{config_version}'?"
            ):
                console.print("[yellow]Deletion cancelled[/yellow]")
                return

        result = client.config.delete(config_version=config_version)

        if not result.success:
            console.print(
                f"[red]✗ Failed to delete configuration profile '{config_version}': {result.error}[/red]"
            )
            sys.exit(1)

        console.print(
            f"[green]✓ Successfully deleted configuration profile: {config_version}[/green]"
        )

    except Exception as e:
        logger.error(f"Error deleting config profile: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="config-sync-bda")
@click.option(
    "--stack-name",
    required=True,
    help="CloudFormation stack name",
)
@click.option(
    "--direction",
    type=click.Choice(["bidirectional", "bda-to-idp", "idp-to-bda"]),
    default="bidirectional",
    help="Sync direction (default: bidirectional)",
)
@click.option(
    "--mode",
    type=click.Choice(["replace", "merge"]),
    default="replace",
    help="Sync mode: 'replace' (full alignment) or 'merge' (additive, don't delete) (default: replace)",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    help="Configuration profile to sync (default: active profile); --config-version is the former name and still works",
)
@click.option("--region", help="AWS region (optional)")
def config_sync_bda(
    stack_name: str,
    direction: str,
    mode: str,
    config_version: Optional[str],
    region: Optional[str],
):
    """
    Synchronize IDP document classes with BDA blueprints

    Performs bidirectional or one-way synchronization between the IDP
    configuration's document classes and BDA (Bedrock Data Automation) blueprints.

    Sync directions:
      bidirectional: Full two-way sync (default)
      bda-to-idp:    Import BDA blueprints into IDP config
      idp-to-bda:    Push IDP classes to BDA blueprints

    Sync modes:
      replace: Target is aligned to match source exactly (default)
      merge:   Source items are added without removing existing items

    Examples:

      # Bidirectional sync (default)
      idp-cli config-sync-bda --stack-name my-stack

      # Import BDA blueprints to IDP
      idp-cli config-sync-bda --stack-name my-stack --direction bda-to-idp

      # Push IDP to BDA (merge mode)
      idp-cli config-sync-bda --stack-name my-stack --direction idp-to-bda --mode merge

      # Sync specific config profile
      idp-cli config-sync-bda --stack-name my-stack --config-profile v2
    """
    try:
        from idp_sdk import IDPClient

        # Normalize direction for SDK (CLI uses dashes, SDK uses underscores)
        sdk_direction = direction.replace("-", "_")

        console.print(f"[bold blue]BDA Sync for stack: {stack_name}[/bold blue]")
        console.print(f"Direction: {direction}")
        console.print(f"Mode: {mode}")
        if config_version:
            console.print(f"Config profile: {config_version}")
        console.print()

        client = IDPClient(stack_name=stack_name, region=region)

        with console.status("[cyan]Synchronizing with BDA...[/cyan]"):
            result = client.config.sync_bda(
                direction=sdk_direction,
                mode=mode,
                config_version=config_version,
            )

        if result.success:
            console.print("[green]✓ BDA sync completed successfully[/green]")
            console.print(f"  Classes synced: {result.classes_synced}")
            if result.processed_classes:
                for cls_name in result.processed_classes:
                    console.print(f"    • {cls_name}")
        else:
            console.print("[yellow]⚠ BDA sync completed with issues[/yellow]")
            console.print(f"  Classes synced: {result.classes_synced}")
            console.print(f"  Classes failed: {result.classes_failed}")
            if result.error:
                console.print(f"  [red]Error: {result.error}[/red]")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error syncing BDA: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="discover")
@click.option(
    "--stack-name",
    help="CloudFormation stack name (optional — if omitted, runs in local mode without saving to config)",
)
@click.option(
    "--document",
    "-d",
    required=True,
    multiple=True,
    type=click.Path(exists=True),
    help="Path to document file(s). Specify multiple times for batch: -d doc1.pdf -d doc2.pdf",
)
@click.option(
    "--ground-truth",
    "-g",
    multiple=True,
    type=click.Path(exists=True),
    help="Path to JSON ground truth file(s). Auto-matched to documents by filename stem",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    help="Configuration profile to save the discovered schema to (default: active profile); --config-version is the former name and still works",
)
@click.option("--region", help="AWS region (optional)")
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output path: file (single doc or JSON array for batch) or directory (one file per schema)",
)
@click.option(
    "--class-hint",
    help="Hint for the document class name (e.g., 'W2 Form'). The LLM will use this as $id.",
)
@click.option(
    "--page-range",
    multiple=True,
    help="Page range to discover (e.g., '1-3'). Repeatable for multi-section. Requires PDF document.",
)
@click.option(
    "--page-label",
    multiple=True,
    help="Label for corresponding --page-range (e.g., 'W2 Form'). Used as class name hint per range.",
)
@click.option(
    "--auto-detect",
    is_flag=True,
    help="Auto-detect document section boundaries using AI, then discover each section.",
)
@click.option(
    "--detect-only",
    is_flag=True,
    help="Only detect section boundaries (use with --auto-detect). Prints boundaries without running discovery.",
)
@click.option(
    "--model-id",
    default=None,
    help=(
        "Override the Bedrock model ID used for discovery "
        "(e.g., 'us.anthropic.claude-opus-4-6-v1'). When omitted, the "
        "discovery model from the stack config (stack mode) or system "
        "defaults (local mode) is used."
    ),
)
def discover(
    stack_name: str,
    document: tuple,
    ground_truth: tuple,
    config_version: Optional[str],
    region: Optional[str],
    output: Optional[str],
    class_hint: Optional[str],
    page_range: tuple,
    page_label: tuple,
    auto_detect: bool,
    detect_only: bool,
    model_id: Optional[str],
):
    """
    Discover document class schema from sample document(s)

    Analyzes document(s) using Amazon Bedrock to automatically generate
    JSON Schema definitions for document classes.

    Ground truth files (-g) are auto-matched to documents (-d) by filename
    stem: invoice.pdf matches invoice.json. Unmatched documents run without
    ground truth.

    Special case: when exactly one document and one ground truth file are
    provided, they are paired by position regardless of filename stem. This
    supports the common case where ground truth files have generic names
    (e.g., baseline/<doc>/sections/1/result.json).

    If any -g files are provided in batch mode (multiple -d or multiple -g)
    and cannot be matched to a document by stem, discover exits non-zero
    with an error rather than silently falling back to no-GT discovery.
    ([#310](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/310))

    For --output (-o) in batch mode: if path is a directory, writes one
    JSON file per schema; if path is a file, writes all schemas as a
    JSON array.

    Examples:

      # Single document
      idp-cli discover -d ./invoice.pdf

      # With ground truth (matched by filename stem)
      idp-cli discover -d ./invoice.pdf -g ./invoice.json

      # With class name hint
      idp-cli discover -d ./form.pdf --class-hint "W2 Tax Form"

      # Multi-section: discover specific page ranges
      idp-cli discover -d ./lending_package.pdf \\
          --page-range "1-2" --page-label "Cover Letter" \\
          --page-range "3-5" --page-label "W2 Form" \\
          -o ./schemas/

      # Auto-detect sections then discover each
      idp-cli discover -d ./lending_package.pdf --auto-detect -o ./schemas/

      # Only detect section boundaries (no discovery)
      idp-cli discover -d ./lending_package.pdf --auto-detect --detect-only

      # Stack mode (saves to config)
      idp-cli discover --stack-name my-stack -d ./invoice.pdf --config-profile v2

      # Override the discovery model (e.g. use Claude Opus instead of the default)
      idp-cli discover -d ./invoice.pdf -g ./invoice.json \\
          --model-id us.anthropic.claude-opus-4-6-v1
    """
    import json
    from pathlib import Path

    try:
        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)

        # --- Auto-detect mode ---
        if auto_detect:
            if len(document) > 1:
                console.print(
                    "[red]✗ Error: --auto-detect works with a single document only[/red]"
                )
                sys.exit(1)

            doc_path = document[0]
            console.print("[bold blue]IDP Discovery — Auto-Detect Sections[/bold blue]")
            if stack_name:
                console.print(f"Stack: {stack_name}")
            console.print(f"Document: {doc_path}")
            if model_id:
                console.print(f"Model ID override: {model_id}")
            console.print()

            if detect_only:
                # Only detect boundaries, don't run discovery
                with console.status(
                    "[cyan]Detecting section boundaries with AI...[/cyan]"
                ):
                    detect_result = client.discovery.auto_detect_sections(
                        document_path=doc_path,
                        model_id=model_id,
                    )

                if detect_result.status != "SUCCESS":
                    console.print(
                        f"[red]✗ Auto-detect failed: {detect_result.error}[/red]"
                    )
                    sys.exit(1)

                console.print(
                    f"[green]✓ Detected {len(detect_result.sections)} section(s):[/green]"
                )
                console.print()
                for s in detect_result.sections:
                    label = s.type or "Unknown"
                    console.print(f"  Pages {s.start}-{s.end}: [cyan]{label}[/cyan]")

                # Print as JSON if output specified
                if output:
                    sections_json = [
                        {"start": s.start, "end": s.end, "type": s.type}
                        for s in detect_result.sections
                    ]
                    with open(output, "w", encoding="utf-8") as f:
                        f.write(json.dumps(sections_json, indent=2))
                    console.print()
                    console.print(
                        f"[green]✓ Section boundaries written to: {output}[/green]"
                    )
                return

            # Auto-detect + discover each section
            console.print("[bold]Step 1: Detecting section boundaries...[/bold]")
            with console.status("[cyan]Detecting section boundaries with AI...[/cyan]"):
                batch_result = client.discovery.run(
                    document_path=doc_path,
                    config_version=config_version,
                    auto_detect=True,
                    model_id=model_id,
                )

            # batch_result is DiscoveryBatchResult
            all_schemas = []
            for r in batch_result.results:
                doc_class = r.document_class or "Unknown"
                range_info = f" (pages {r.page_range})" if r.page_range else ""
                if r.status == "SUCCESS":
                    console.print(f"  [green]✓ {doc_class}{range_info}[/green]")
                    if r.json_schema:
                        all_schemas.append(r.json_schema)
                else:
                    console.print(f"  [red]✗ Failed{range_info}: {r.error}[/red]")

            console.print()
            console.print(
                f"[bold]Summary:[/bold] {batch_result.succeeded}/{batch_result.total} succeeded"
            )

            # Write output
            _write_discover_output(output, all_schemas, console)

            if stack_name and config_version and batch_result.succeeded > 0:
                console.print(
                    f"[green]✓ Schema(s) saved to configuration"
                    f" (version: {config_version})[/green]"
                )

            if batch_result.failed > 0:
                sys.exit(1)
            return

        # --- Multi-section page range mode ---
        if page_range:
            if len(document) > 1:
                console.print(
                    "[red]✗ Error: --page-range works with a single document only[/red]"
                )
                sys.exit(1)

            doc_path = document[0]
            console.print("[bold blue]IDP Discovery — Multi-Section[/bold blue]")
            if stack_name:
                console.print(f"Stack: {stack_name}")
            console.print(f"Document: {doc_path}")
            console.print(f"Page ranges: {len(page_range)}")
            if model_id:
                console.print(f"Model ID override: {model_id}")
            console.print()

            # Build page_ranges list
            page_ranges_list = []
            for idx, pr in enumerate(page_range):
                label = page_label[idx] if idx < len(page_label) else None
                # Parse "start-end" format
                parts = pr.strip().split("-")
                start = int(parts[0])
                end = int(parts[1]) if len(parts) > 1 else start
                page_ranges_list.append({"start": start, "end": end, "label": label})
                label_str = f" → {label}" if label else ""
                console.print(f"  Range {idx + 1}: pages {start}-{end}{label_str}")

            console.print()

            with console.status(
                "[cyan]Analyzing sections with Amazon Bedrock...[/cyan]"
            ):
                batch_result = client.discovery.run_multi_section(
                    document_path=doc_path,
                    page_ranges=page_ranges_list,
                    config_version=config_version,
                    model_id=model_id,
                )

            all_schemas = []
            for r in batch_result.results:
                doc_class = r.document_class or "Unknown"
                range_info = f" (pages {r.page_range})" if r.page_range else ""
                if r.status == "SUCCESS":
                    console.print(f"  [green]✓ {doc_class}{range_info}[/green]")
                    if r.json_schema:
                        all_schemas.append(r.json_schema)
                else:
                    console.print(f"  [red]✗ Failed{range_info}: {r.error}[/red]")

            console.print()
            console.print(
                f"[bold]Summary:[/bold] {batch_result.succeeded}/{batch_result.total} succeeded"
            )

            _write_discover_output(output, all_schemas, console)

            if stack_name and config_version and batch_result.succeeded > 0:
                console.print(
                    f"[green]✓ Schema(s) saved to configuration"
                    f" (version: {config_version})[/green]"
                )

            if batch_result.failed > 0:
                sys.exit(1)
            return

        # --- Standard discovery mode (original logic) ---
        # Special case: exactly one document + one ground truth → pair by
        # position regardless of filename stem. This supports the common case
        # where GT files have generic names (e.g. baseline/<doc>/sections/1/result.json).
        # See issue #310.
        if len(document) == 1 and len(ground_truth) == 1:
            doc_gt_pairs = [(document[0], ground_truth[0])]
        else:
            # Build ground truth map: filename stem → gt path.
            # Detect duplicate stems up front — silently overwriting them
            # would hide user errors (e.g. two baseline/.../result.json files).
            gt_map: dict = {}
            duplicate_stems: dict = {}
            for gt_path in ground_truth:
                stem = Path(gt_path).stem
                if stem in gt_map:
                    duplicate_stems.setdefault(stem, [gt_map[stem]]).append(gt_path)
                else:
                    gt_map[stem] = gt_path

            if duplicate_stems:
                console.print(
                    "[red]✗ Error: multiple ground truth files share the same filename stem. "
                    "Discover cannot determine which document each belongs to.[/red]"
                )
                for stem, paths in duplicate_stems.items():
                    console.print(f"  stem '{stem}':")
                    for p in paths:
                        console.print(f"    - {p}")
                console.print(
                    "[yellow]Hint: rename ground truth files to uniquely match each document's "
                    "filename stem, or run discover separately for each document.[/yellow]"
                )
                sys.exit(1)

            # Match ground truth to documents by filename stem
            doc_gt_pairs = []
            for doc_path in document:
                doc_stem = Path(doc_path).stem
                matched_gt = gt_map.pop(doc_stem, None)
                doc_gt_pairs.append((doc_path, matched_gt))

            # Unmatched ground truth files are a fatal error when -g was
            # explicitly provided. Previously this was a yellow warning that
            # silently fell back to without-GT discovery — which violated the
            # user's explicit request and produced subtly worse results that
            # were hard to diagnose. See issue #310.
            if gt_map:
                console.print(
                    "[red]✗ Error: ground truth file(s) could not be matched to "
                    "any document by filename stem:[/red]"
                )
                for gt_stem, gt_path in gt_map.items():
                    console.print(f"    - {gt_path} (stem: '{gt_stem}')")
                doc_stems = sorted({Path(p).stem for p in document})
                console.print(f"  Document stems available: {doc_stems}")
                console.print(
                    "[yellow]Hint: for a single document + single ground truth, "
                    "filenames don't need to match (they will be paired by position). "
                    "For batch mode, rename each ground truth file to match its "
                    "corresponding document's stem.[/yellow]"
                )
                sys.exit(1)

        # Header
        is_batch = len(document) > 1
        if is_batch:
            console.print("[bold blue]IDP Discovery (batch)[/bold blue]")
        else:
            console.print("[bold blue]IDP Discovery[/bold blue]")
        if stack_name:
            console.print(f"Stack: {stack_name}")
        console.print(f"Documents: {len(document)}")
        gt_matched = sum(1 for _, gt in doc_gt_pairs if gt)
        if gt_matched:
            console.print(f"Ground truth matched: {gt_matched}/{len(document)}")
        if class_hint:
            console.print(f"Class hint: {class_hint}")
        if config_version:
            console.print(f"Config profile: {config_version}")
        if model_id:
            console.print(f"Model ID override: {model_id}")
        console.print()

        # Process documents
        succeeded = 0
        failed = 0
        all_schemas = []

        for i, (doc_path, matched_gt) in enumerate(doc_gt_pairs, 1):
            if is_batch:
                gt_info = (
                    f" [dim](with GT: {Path(matched_gt).name})[/dim]"
                    if matched_gt
                    else ""
                )
                console.print(
                    f"[bold cyan]Processing {i}/{len(document)}: {doc_path}{gt_info}[/bold cyan]"
                )
            else:
                console.print(f"Document: {doc_path}")
                if matched_gt:
                    console.print(f"Ground Truth: {matched_gt}")
                console.print()

            with console.status("[cyan]Analyzing with Amazon Bedrock...[/cyan]"):
                result = client.discovery.run(
                    document_path=doc_path,
                    ground_truth_path=matched_gt,
                    config_version=config_version,
                    class_name_hint=class_hint,
                    model_id=model_id,
                )

            if result.status == "SUCCESS":
                doc_class = result.document_class or "Unknown"
                if is_batch:
                    console.print(f"  [green]✓ Discovered class: {doc_class}[/green]")
                else:
                    console.print("[green]✓ Discovery completed successfully[/green]")
                    console.print()
                    if result.document_class:
                        console.print(
                            f"[bold]Document Class:[/bold] {result.document_class}"
                        )
                    if result.json_schema:
                        properties = result.json_schema.get("properties", {})
                        console.print(
                            f"[bold]Properties:[/bold] {len(properties)} top-level fields"
                        )
                        console.print()
                        console.print("[bold]Generated JSON Schema:[/bold]")
                        console.print(json.dumps(result.json_schema, indent=2))
                        console.print()

                if result.json_schema:
                    all_schemas.append(result.json_schema)
                succeeded += 1
            else:
                if is_batch:
                    console.print(f"  [red]✗ Failed: {result.error}[/red]")
                else:
                    console.print("[red]✗ Discovery failed[/red]")
                    if result.error:
                        console.print(f"[red]Error: {result.error}[/red]")
                failed += 1

            if is_batch:
                console.print()

        # Write/print output
        _write_discover_output(output, all_schemas, console, is_batch)

        # Summary
        if is_batch:
            console.print("[bold]Summary:[/bold]")
            console.print(
                f"  Total: {len(document)}, Succeeded: {succeeded}, Failed: {failed}"
            )
            console.print("[green]✓ Batch discovery complete[/green]")

        if stack_name and config_version and succeeded > 0:
            console.print(
                f"[green]✓ Schema(s) saved to configuration"
                f" (version: {config_version})[/green]"
            )

        if failed > 0:
            sys.exit(1)

    except FileNotFoundError as e:
        console.print(f"[red]✗ File not found: {e}[/red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


def _write_discover_output(output, all_schemas, console, is_batch=True):
    """Helper to write discovery output to file or stdout."""
    import json
    from pathlib import Path

    if not all_schemas:
        return

    if not output and is_batch:
        # No -o specified in batch mode → print schemas to stdout
        console.print()
        console.print("[bold]Discovered schemas:[/bold]")
        if len(all_schemas) == 1:
            console.print(json.dumps(all_schemas[0], indent=2))
        else:
            console.print(json.dumps(all_schemas, indent=2))
        console.print()
    elif output:
        output_path = Path(output)
        if len(all_schemas) == 1 and not output_path.is_dir():
            # Single schema → write directly to file
            with open(output, "w", encoding="utf-8") as f:
                f.write(json.dumps(all_schemas[0], indent=2))
            console.print(f"[green]✓ Schema written to: {output}[/green]")
        elif output_path.is_dir() or (is_batch and not output_path.suffix):
            # Directory mode → one file per schema
            output_path.mkdir(parents=True, exist_ok=True)
            for schema in all_schemas:
                class_name = (
                    schema.get("$id")
                    or schema.get("x-aws-idp-document-type")
                    or "unknown"
                )
                file_path = output_path / f"{class_name}.json"
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(schema, indent=2))
                console.print(f"[green]✓ Schema written to: {file_path}[/green]")
        else:
            # File mode with multiple schemas → JSON array
            with open(output, "w", encoding="utf-8") as f:
                f.write(json.dumps(all_schemas, indent=2))
            console.print(
                f"[green]✓ {len(all_schemas)} schemas written to: {output}[/green]"
            )


@cli.command(name="discover-multidoc")
@click.option(
    "--dir",
    "document_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Directory containing documents to analyze (recursive scan)",
)
@click.option(
    "--document",
    "-d",
    "documents",
    multiple=True,
    type=click.Path(exists=True),
    help="Individual document files (repeatable: -d doc1.pdf -d doc2.png)",
)
@click.option(
    "--embedding-model",
    default=None,
    help="Bedrock embedding model ID (default: us.cohere.embed-v4:0)",
)
@click.option(
    "--analysis-model",
    default=None,
    help="Bedrock LLM for cluster analysis (default: us.anthropic.claude-sonnet-4-6)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output directory for discovered JSON schemas",
)
@click.option(
    "--stack-name",
    default=None,
    help="CloudFormation stack name (required for --save-to-config)",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    default=None,
    help="Configuration profile to save schemas to; --config-version is the former name and still works",
)
@click.option(
    "--save-to-config",
    is_flag=True,
    default=False,
    help="Save discovered schemas to the stack's configuration",
)
@click.option("--region", default=None, help="AWS region")
def multi_discover(
    document_dir,
    documents,
    embedding_model,
    analysis_model,
    output,
    stack_name,
    config_version,
    save_to_config,
    region,
):
    """Discover document classes from a collection of documents.

    Analyzes a directory of documents using embedding-based clustering and
    agentic analysis to automatically discover document classes and generate
    JSON Schemas.

    Requires: make setup (or: pip install -e 'lib/idp_common_pkg[multi_document_discovery]')

    Note: Requires at least 2 documents per expected class. Clusters with
    fewer than 2 documents are filtered as noise. For discovering schemas
    from individual documents, use 'idp-cli discover' instead.

    \b
    Examples:
      # Discover from a directory of documents
      idp-cli discover-multidoc --dir ./samples/

      # Discover with explicit files
      idp-cli discover-multidoc -d doc1.pdf -d doc2.png -d doc3.jpg

      # Save schemas to output directory
      idp-cli discover-multidoc --dir ./samples/ -o ./schemas/

      # Save to stack configuration
      idp-cli discover-multidoc --dir ./samples/ --save-to-config \\
          --stack-name IDP --config-profile v2
    """
    import json

    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.table import Table

    console = Console()

    if not document_dir and not documents:
        console.print(
            "[red]Error: Either --dir or --document/-d must be provided[/red]"
        )
        sys.exit(1)

    if save_to_config and not stack_name:
        console.print(
            "[red]Error: --stack-name is required when using --save-to-config[/red]"
        )
        sys.exit(1)

    if save_to_config and not config_version:
        console.print(
            "[red]Error: --config-profile is required when using --save-to-config[/red]"
        )
        sys.exit(1)

    try:
        from idp_sdk import IDPClient
    except ImportError as exc:
        # Never suggest `pip install idp-sdk`: idp-sdk is first-party (lib/idp_sdk)
        # and the PyPI name is a third-party squat. Point at the local install.
        console.print(
            "[red]Error: idp-sdk is required. Install it from the local checkout "
            "with 'make setup' (or: pip install -e lib/idp_sdk).[/red]"
        )
        console.print(f"[red]Underlying import error: {exc}[/red]")
        sys.exit(1)

    client = IDPClient(stack_name=stack_name, region=region)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Starting multi-document discovery...", total=None)

        def _progress_callback(step: str, data=None):
            data = data or {}
            if step == "documents_found":
                progress.update(
                    task,
                    description=f"Found {data.get('count', '?')} documents",
                )
            elif step == "generating_embeddings":
                progress.update(
                    task,
                    description=(
                        f"Generating embeddings for {data.get('total', '?')} documents..."
                    ),
                )
            elif step == "embedding_progress":
                done = data.get("done", 0)
                total = data.get("total", 0)
                progress.update(
                    task,
                    description=f"Embedding documents... {done}/{total}",
                )
            elif step == "clustering":
                progress.update(
                    task,
                    description=(
                        f"Clustering {data.get('num_documents', '?')} documents..."
                    ),
                )
            elif step == "clustering_complete":
                progress.update(
                    task,
                    description=(f"Found {data.get('num_clusters', '?')} clusters"),
                )
            elif step == "analyzing_clusters":
                progress.update(
                    task,
                    description=(
                        f"Analyzing {data.get('total', '?')} clusters with AI agent..."
                    ),
                )
            elif step == "cluster_analysis_progress":
                done = data.get("done", 0)
                total = data.get("total", 0)
                cls = data.get("classification", "")
                label = f" → {cls}" if cls else ""
                progress.update(
                    task,
                    description=f"Analyzing clusters... {done}/{total}{label}",
                )
            elif step == "reflecting":
                progress.update(task, description="Generating reflection report...")
            elif step == "saving_to_config":
                progress.update(
                    task,
                    description=(
                        f"Saving to config profile '{data.get('version', '?')}'..."
                    ),
                )
            elif step == "pipeline_complete":
                progress.update(task, description="Pipeline complete ✓")

        result = client.discovery.run_multi_doc(
            document_dir=document_dir,
            document_paths=list(documents) if documents else None,
            embedding_model_id=embedding_model,
            analysis_model_id=analysis_model,
            save_to_config=save_to_config,
            config_version=config_version,
            output_dir=output,
            progress_callback=_progress_callback,
            region=region,
        )

    # Display results
    console.print()

    if result.status == "FAILED":
        console.print(f"[red]✗ Discovery failed: {result.error}[/red]")
        sys.exit(1)

    # Summary table
    table = Table(title="Multi-Document Discovery Results", show_lines=True)
    table.add_column("Cluster", style="cyan", justify="center")
    table.add_column("Classification", style="bold green")
    table.add_column("Documents", style="yellow", justify="center")
    table.add_column("Fields", style="magenta", justify="center")
    table.add_column("Status", justify="center")

    for dc in result.discovered_classes:
        if dc.error:
            table.add_row(
                str(dc.cluster_id),
                "—",
                str(dc.document_count),
                "—",
                "[red]✗ Error[/red]",
            )
        else:
            num_fields = (
                len(dc.json_schema.get("properties", {})) if dc.json_schema else 0
            )
            table.add_row(
                str(dc.cluster_id),
                dc.classification or "Unknown",
                str(dc.document_count),
                str(num_fields),
                "[green]✓[/green]",
            )

    console.print(table)
    console.print()

    # Stats line
    console.print(
        f"[bold]Summary:[/bold] {result.total_documents} documents → "
        f"{result.total_clusters} clusters → "
        f"{len([c for c in result.discovered_classes if not c.error])} schemas"
    )

    if result.noise_documents > 0:
        console.print(
            f"[yellow]⚠ {result.noise_documents} documents failed embedding[/yellow]"
        )

    if result.config_version:
        console.print(
            f"[green]✓ Schemas saved to config profile: {result.config_version}[/green]"
        )

    if output:
        console.print(f"[green]✓ Schemas written to: {output}[/green]")

    # Print schemas to stdout if no output specified
    if not output and not save_to_config:
        all_schemas = [
            dc.json_schema
            for dc in result.discovered_classes
            if dc.json_schema and not dc.error
        ]
        if all_schemas:
            console.print()
            console.print("[bold]Discovered schemas:[/bold]")
            console.print(json.dumps(all_schemas, indent=2))

    # Print reflection report if available
    if result.reflection_report:
        console.print()
        console.print("[bold]Reflection Report:[/bold]")
        from rich.markdown import Markdown

        console.print(Markdown(result.reflection_report))

    if result.status == "PARTIAL":
        console.print(
            "\n[yellow]⚠ Some clusters failed analysis. "
            "Check the errors above.[/yellow]"
        )
        sys.exit(1)


@cli.command()
@click.option(
    "--source-dir",
    default=".",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Path to IDP project root directory",
)
@click.option(
    "--bucket-basename",
    default=None,
    help="S3 bucket basename for artifacts — region is appended automatically (auto-generated if not provided)",
)
@click.option(
    "--prefix", default=None, help="S3 key prefix for artifacts (default: idp-cli)"
)
@click.option("--region", required=True, help="AWS region for deployment")
@click.option(
    "--headless", is_flag=True, help="Also generate a headless (no-UI) template variant"
)
@click.option(
    "--govcloud",
    is_flag=True,
    help=(
        "Also generate a GovCloud template variant: removes all AWS::CloudFront::* "
        "resources (unavailable in GovCloud) and forces API Gateway Web UI hosting. "
        "Keeps the full UI."
    ),
)
@click.option("--public", is_flag=True, help="Make S3 artifacts publicly readable")
@click.option(
    "--max-workers",
    type=int,
    default=None,
    help="Maximum concurrent build workers (default: auto-detect)",
)
@click.option(
    "--clean-build",
    is_flag=True,
    help="Delete all checksum files to force full rebuild",
)
@click.option(
    "--no-validate", is_flag=True, help="Skip CloudFormation template validation"
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose build output")
@click.option(
    "--lint/--no-lint",
    default=True,
    help="Enable/disable ruff linting and cfn-lint (default: enabled)",
)
def publish(
    source_dir: str,
    bucket_basename: Optional[str],
    prefix: Optional[str],
    region: str,
    headless: bool,
    govcloud: bool,
    public: bool,
    max_workers: Optional[int],
    clean_build: bool,
    no_validate: bool,
    verbose: bool,
    lint: bool,
):
    """
    Build, package, and publish IDP CloudFormation artifacts to S3

    This command is the CLI equivalent of publish.py. It builds all Lambda
    functions, Lambda layers, SAM templates, packages the UI, and uploads
    everything to S3. Optionally generates a headless template variant.

    Examples:

      # Standard build and publish
      idp-cli publish --source-dir . --region us-east-1

      # With custom bucket and prefix
      idp-cli publish --source-dir . --bucket my-artifacts --prefix v1 --region us-east-1

      # Build with headless template
      idp-cli publish --source-dir . --region us-east-1 --headless

      # Public artifacts (for shared deployments)
      idp-cli publish --source-dir . --region us-east-1 --public

      # Full rebuild with verbose output
      idp-cli publish --source-dir . --region us-east-1 --clean-build --verbose

      # Skip validation
      idp-cli publish --source-dir . --region us-east-1 --no-validate
    """
    try:
        client = IDPClient(region=region)
        result = client.publish.build(
            source_dir=source_dir,
            bucket=bucket_basename,
            prefix=prefix,
            region=region,
            headless=headless,
            govcloud=govcloud,
            public=public,
            max_workers=max_workers,
            clean_build=clean_build,
            no_validate=no_validate,
            verbose=verbose,
            lint=lint,
        )

        if not result.success:
            console.print(f"[red]✗ Publish failed: {result.error}[/red]")
            sys.exit(1)

        # Print deployment URLs
        console.print()
        client.publish.print_deployment_urls(
            template_url=result.template_url or "",
            region=region,
            headless_template_url=result.headless_template_url,
            govcloud_template_url=result.govcloud_template_url,
        )
        console.print()
        console.print("[bold green]✅ Publish complete![/bold green]")

    except Exception as e:
        logger.error(f"Error during publish: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option("--region", default=None, help="AWS region")
@click.option(
    "--prompt",
    default=None,
    help="Single-shot prompt (non-interactive mode)",
)
@click.option(
    "--enable-code-intelligence",
    is_flag=True,
    default=False,
    help="Enable Code Intelligence Agent (uses external third-party services)",
)
def chat(
    stack_name: str,
    region: Optional[str],
    prompt: Optional[str],
    enable_code_intelligence: bool,
):
    """Interactive Agent Companion Chat from the terminal.

    Provides access to the full multi-agent orchestrator including
    Analytics, Error Analyzer, and other agents.

    \b
    Examples:
      # Interactive mode
      idp-cli chat --stack-name IDP

      # Single-shot mode
      idp-cli chat --stack-name IDP --prompt "How many documents were processed today?"

      # With Code Intelligence (external services)
      idp-cli chat --stack-name IDP --enable-code-intelligence
    """
    try:
        from .chat import run_chat
    except ImportError:
        console.print(
            "[red]✗ Chat requires idp_common[agents] to be installed.\n"
            "  Run: pip install -e 'lib/idp_common_pkg[agents]'[/red]"
        )
        sys.exit(1)

    run_chat(
        stack_name=stack_name,
        region=region,
        prompt=prompt,
        enable_code_intelligence=enable_code_intelligence,
    )


@cli.command(name="test-result")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option("--test-run-id", required=True, help="Test run ID")
@click.option("--region", default=None, help="AWS region")
@click.option("--wait", is_flag=True, help="Wait for evaluation to complete")
@click.option(
    "--timeout", default=600, type=int, help="Timeout in seconds (default: 600)"
)
@click.option(
    "--output-dir",
    type=click.Path(),
    help="Output directory for saving results JSON file",
)
def test_result(
    stack_name: str,
    test_run_id: str,
    region: Optional[str],
    wait: bool,
    timeout: int,
    output_dir: Optional[str],
):
    """Get test results for a specific test run.

    Invokes TestResultsResolverFunction to trigger evaluation if needed
    and retrieve test results including accuracy, precision, recall, F1 score, and cost.

    \b
    Examples:
      # Get results immediately (may show evaluating status)
      idp-cli test-result --stack-name my-stack --test-run-id fake-w2-20260409-123456

      # Wait for evaluation to complete
      idp-cli test-result --stack-name my-stack --test-run-id fake-w2-20260409-123456 --wait --timeout 900

      # Save results to file
      idp-cli test-result --stack-name my-stack --test-run-id fake-w2-20260409-123456 --wait --output-dir ./results
    """
    if not region:
        region = os.environ.get("AWS_REGION", "us-east-1")

    try:
        # Use SDK to get test result
        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)

        if wait:
            console.print(
                f"[yellow]⏳ Waiting for test run to complete (up to {timeout}s)...[/yellow]"
            )

        test_result = client.testing.get_test_result(
            test_run_id=test_run_id,
            wait=wait,
            timeout=timeout,
            poll_interval=10,
        )

        # Save to file if output-dir specified
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            output_file = os.path.join(output_dir, f"{test_run_id}-result.json")
            with open(output_file, "w") as f:
                # Use raw_data for complete JSON export
                json.dump(test_result.raw_data, f, indent=2, default=str)
            console.print(f"[green]✓ Results saved to: {output_file}[/green]\n")

        # Display results using Rich formatting
        console.print("\n[bold green]✓ Test Results[/bold green]\n")
        console.print(f"[bold]Test Run:[/bold] {test_result.test_run_id}")
        console.print(f"[bold]Test Set:[/bold] {test_result.test_set_name}")
        console.print(f"[bold]Status:[/bold] {test_result.status}")
        console.print(
            f"[bold]Files:[/bold] {test_result.completed_files}/{test_result.files_count} completed"
        )

        if test_result.failed_files > 0:
            console.print(
                f"[bold red]Failed Files:[/bold red] {test_result.failed_files}"
            )

        console.print()

        if test_result.overall_accuracy is not None:
            console.print(
                f"[bold cyan]Overall Accuracy:[/bold cyan] {test_result.overall_accuracy:.2%}"
            )

        if test_result.accuracy_breakdown:
            breakdown = test_result.accuracy_breakdown

            # ``.get(k, 0)`` doesn't help when the key IS present with
            # value ``None`` — the new Optional[float] contract on
            # accuracy_breakdown puts None at present keys on any
            # zero-denominator or error path, and ``.2%`` on None
            # raises TypeError.
            def _fmt_pct(v):
                return f"{v:.2%}" if isinstance(v, (int, float)) else "N/A"

            console.print(
                f"[bold cyan]Precision:[/bold cyan] {_fmt_pct(breakdown.get('precision'))}"
            )
            console.print(
                f"[bold cyan]Recall:[/bold cyan] {_fmt_pct(breakdown.get('recall'))}"
            )
            console.print(
                f"[bold cyan]F1 Score:[/bold cyan] {_fmt_pct(breakdown.get('f1_score'))}"
            )

        if test_result.total_cost:
            console.print(
                f"[bold yellow]Total Cost:[/bold yellow] ${test_result.total_cost:.4f}"
            )

        console.print()

        if test_result.created_at:
            console.print(f"[dim]Created: {test_result.created_at}[/dim]")
        if test_result.completed_at:
            console.print(f"[dim]Completed: {test_result.completed_at}[/dim]")

    except Exception as e:
        logger.error(f"Error getting test results: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="test-compare")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--test-run-ids",
    required=True,
    help="Comma-separated list of test run IDs to compare",
)
@click.option("--region", default=None, help="AWS region")
@click.option(
    "--output-dir",
    type=click.Path(),
    help="Output directory for saving comparison JSON and CSV files",
)
def test_compare(
    stack_name: str, test_run_ids: str, region: Optional[str], output_dir: Optional[str]
):
    """Compare results from multiple test runs.

    Shows side-by-side comparison of metrics and configuration differences
    between test runs.

    \b
    Examples:
      # Compare two test runs
      idp-cli test-compare --stack-name my-stack \\
        --test-run-ids "fake-w2-20260409-123456,fake-w2-20260409-234567"

      # Compare and save to files
      idp-cli test-compare --stack-name my-stack \\
        --test-run-ids "run1,run2,run3" --output-dir ./comparisons
    """
    import csv
    from datetime import datetime, timezone

    if not region:
        region = os.environ.get("AWS_REGION", "us-east-1")

    try:
        # Parse test run IDs
        test_run_id_list = [tid.strip() for tid in test_run_ids.split(",")]

        if len(test_run_id_list) < 2:
            console.print(
                "[red]✗ At least 2 test run IDs required for comparison[/red]"
            )
            sys.exit(1)

        # Use SDK to compare test runs
        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)

        console.print(f"[blue]Comparing {len(test_run_id_list)} test runs...[/blue]\n")

        comparison_result = client.testing.compare_test_runs(
            test_run_ids=test_run_id_list
        )

        metrics = comparison_result.metrics
        # Note: configs not yet in SDK model, but in raw_data if needed
        configs = []  # TODO: Add to SDK model if needed

        if not metrics:
            console.print("[yellow]⚠ No metrics data available for comparison[/yellow]")
            sys.exit(1)

        # Save to files if output-dir specified
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

            # Save JSON (reconstruct result dict for compatibility)
            result_data = {
                "metrics": metrics,
                "configs": configs,
            }
            json_file = os.path.join(output_dir, f"comparison-{timestamp}.json")
            with open(json_file, "w") as f:
                json.dump(result_data, f, indent=2, default=str)
            console.print(f"[green]✓ Comparison JSON saved to: {json_file}[/green]")

            # Save CSV (metrics only)
            csv_file = os.path.join(output_dir, f"comparison-{timestamp}.csv")
            with open(csv_file, "w", newline="") as f:
                writer = csv.writer(f)

                # Header row
                header = ["Metric"] + [tid[:30] for tid in test_run_id_list]
                writer.writerow(header)

                # Metric rows
                metric_names = [
                    ("Overall Accuracy", "overallAccuracy"),
                    ("Precision", "accuracyBreakdown.precision"),
                    ("Recall", "accuracyBreakdown.recall"),
                    ("F1 Score", "accuracyBreakdown.f1_score"),
                    ("Total Cost", "totalCost"),
                    ("Files Completed", "completedFiles"),
                    ("Files Failed", "failedFiles"),
                ]

                for display_name, metric_path in metric_names:
                    row = [display_name]
                    for test_run_id in test_run_id_list:
                        test_data = metrics.get(test_run_id, {})

                        # Navigate nested paths
                        value = test_data
                        for key in metric_path.split("."):
                            value = value.get(key) if isinstance(value, dict) else None

                        if value is not None:
                            row.append(
                                f"{value:.4f}"
                                if isinstance(value, float)
                                else str(value)
                            )
                        else:
                            row.append("N/A")

                    writer.writerow(row)

            console.print(f"[green]✓ Comparison CSV saved to: {csv_file}[/green]\n")

        # Display metrics comparison table
        console.print("[bold green]Metrics Comparison[/bold green]\n")

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Metric", style="dim")

        for test_run_id in test_run_id_list:
            table.add_column(test_run_id[:20], justify="right")

        # Add rows for each metric
        metric_names = [
            ("Overall Accuracy", "overallAccuracy", "%"),
            ("Precision", "accuracyBreakdown.precision", "%"),
            ("Recall", "accuracyBreakdown.recall", "%"),
            ("F1 Score", "accuracyBreakdown.f1_score", "%"),
            ("Total Cost", "totalCost", "$"),
        ]

        for display_name, metric_path, format_type in metric_names:
            row = [display_name]
            for test_run_id in test_run_id_list:
                test_data = metrics.get(test_run_id, {})

                # Navigate nested paths
                value = test_data
                for key in metric_path.split("."):
                    value = value.get(key) if isinstance(value, dict) else None

                if value is not None:
                    if format_type == "%":
                        row.append(f"{value:.2%}")
                    elif format_type == "$":
                        row.append(f"${value:.4f}")
                    else:
                        row.append(str(value))
                else:
                    row.append("N/A")

            table.add_row(*row)

        console.print(table)
        console.print()

        # Display configuration differences
        if configs and len(configs) > 0:
            console.print("[bold green]Configuration Differences[/bold green]\n")

            config_table = Table(show_header=True, header_style="bold cyan")
            config_table.add_column("Setting", style="dim")

            for test_run_id in test_run_id_list:
                config_table.add_column(test_run_id[:20])

            for diff in configs:
                setting = diff.get("setting", "")
                values = diff.get("values", {})

                row = [setting]
                for test_run_id in test_run_id_list:
                    value = values.get(test_run_id, "<missing>")
                    # Truncate long values
                    if len(str(value)) > 50:
                        value = str(value)[:47] + "..."
                    row.append(str(value))

                config_table.add_row(*row)

            console.print(config_table)
        else:
            console.print("[dim]No configuration differences to display[/dim]")

        console.print()

    except Exception as e:
        logger.error(f"Error comparing test runs: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="abort-test-run")
@click.option("--stack-name", required=True, help="CloudFormation stack name")
@click.option(
    "--test-run-ids",
    required=True,
    help="Comma-separated list of test run IDs to abort",
)
@click.option("--region", default=None, help="AWS region")
@click.option(
    "--force",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt",
)
def abort_test_run(
    stack_name: str,
    test_run_ids: str,
    region: Optional[str],
    force: bool,
):
    """Abort one or more Test Studio runs.

    Stops all running document workflows and updates test run status to ABORTED.
    Only test runs with status QUEUED or RUNNING can be aborted. Completed
    documents within a test run are preserved.

    \b
    Examples:
      # Abort a single test run
      idp-cli abort-test-run --stack-name my-stack \\
        --test-run-ids "fake-w2-20260409-123456"

      # Abort multiple test runs
      idp-cli abort-test-run --stack-name my-stack \\
        --test-run-ids "run1,run2,run3"

      # Skip confirmation
      idp-cli abort-test-run --stack-name my-stack \\
        --test-run-ids "run1" --force
    """
    if not region:
        region = os.environ.get("AWS_REGION", "us-east-1")

    try:
        # Parse test run IDs
        test_run_id_list = [tid.strip() for tid in test_run_ids.split(",")]

        if not test_run_id_list:
            console.print("[red]✗ No test run IDs provided[/red]")
            sys.exit(1)

        # Show warning and confirm
        if not force:
            console.print()
            console.print("[bold yellow]⚠️  WARNING: Abort Test Runs[/bold yellow]")
            console.print("━" * 60)
            console.print(f"Stack: [cyan]{stack_name}[/cyan]")
            console.print(f"Region: {region}")
            console.print(f"Test Runs: {len(test_run_id_list)}")
            for test_run_id in test_run_id_list:
                console.print(f"  • {test_run_id}")
            console.print()
            console.print(
                "[yellow]This will stop all running document workflows and mark test runs as ABORTED.[/yellow]"
            )
            console.print("[yellow]Completed documents will be preserved.[/yellow]")
            console.print()

            if not click.confirm("Do you want to proceed?"):
                console.print("[yellow]Aborted by user[/yellow]")
                sys.exit(0)

        # Use SDK to abort test runs
        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)

        console.print(f"[blue]Aborting {len(test_run_id_list)} test run(s)...[/blue]\n")

        result = client.testing.abort_test_run(test_run_ids=test_run_id_list)

        # Display results
        if result.get("success"):
            aborted_count = result.get("abortedCount", 0)
            failed_count = result.get("failedCount", 0)

            console.print(f"[green]✓ {result.get('message')}[/green]")

            if aborted_count > 0:
                console.print(
                    f"[green]  Successfully aborted: {aborted_count} test run(s)[/green]"
                )

            if failed_count > 0:
                console.print(
                    f"[yellow]  Failed to abort: {failed_count} test run(s)[/yellow]"
                )

                errors = result.get("errors", [])
                if errors:
                    console.print("\n[bold red]Errors:[/bold red]")
                    for error in errors:
                        console.print(f"  • {error}")
        else:
            console.print(f"[red]✗ Failed: {result.get('message')}[/red]")

            errors = result.get("errors", [])
            if errors:
                console.print("\n[bold red]Errors:[/bold red]")
                for error in errors:
                    console.print(f"  • {error}")

            sys.exit(1)

    except Exception as e:
        logger.error(f"Error aborting test runs: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


@cli.command(name="bootstrap")
@click.option(
    "--prompt",
    "-p",
    required=True,
    help="Natural-language description of the document type to bootstrap",
)
@click.option(
    "--stack-name",
    help="CloudFormation stack name (omit for local mode: print schema, no save)",
)
@click.option("--class-name", help="Document class name to use as $id / document type")
@click.option(
    "--field-hint",
    multiple=True,
    help="A field the schema must include. Repeatable: --field-hint X --field-hint Y",
)
@click.option(
    "--config-profile",
    "--config-version",
    "config_version",
    help="Existing configuration profile to source catalog classes from / merge into; --config-version is the former name and still works",
)
@click.option(
    "--target-profile",
    "--target-version",
    "target_version",
    help="Name of the configuration profile to create (default: bootstrap-<class>); --target-version is the former name and still works",
)
@click.option(
    "--count", "-c", default=3, show_default=True, help="Documents to generate"
)
@click.option(
    "--threshold",
    default=7,
    show_default=True,
    help="Generation quality threshold (1-10)",
)
@click.option(
    "--augment", is_flag=True, help="Apply image augmentation to generated docs"
)
@click.option("--model-id", help="Bedrock model id override for authoring/generation")
@click.option("--region", help="AWS region (optional)")
def bootstrap(
    prompt,
    stack_name,
    class_name,
    field_hint,
    config_version,
    target_version,
    count,
    threshold,
    augment,
    model_id,
    region,
):
    """Bootstrap a config (and test set) from a prompt

    Authors a document-class schema from your description (reusing a catalog
    match when one fits), creates a config profile, and — when the document
    generator is available — generates a labeled synthetic test set.

    Local mode (no --stack-name) authors and prints the schema without saving.
    """
    import os as _os

    from idp_common.synthesis import bootstrap as bootstrap_mod
    from idp_common.synthesis import engine as synthesis_engine

    console.print("[bold blue]IDP Config Bootstrap[/bold blue]")
    console.print(f"Prompt: {prompt}")
    if stack_name:
        console.print(f"Stack: {stack_name}")
    else:
        console.print("[yellow]Local mode — schema will not be saved[/yellow]")

    available, reason = synthesis_engine.generator_available()
    if not available:
        console.print(
            f"[yellow]Note: document generator unavailable ({reason}).[/yellow]"
        )
        console.print(f"[yellow]{synthesis_engine.INSTALL_HINT}[/yellow]")

    request = bootstrap_mod.BootstrapRequest(
        prompt=prompt,
        class_name=class_name,
        field_hints=list(field_hint),
        config_version=config_version,
        target_version=target_version,
        doc_count=count,
        quality_threshold=threshold,
        augment=augment,
        model_id=model_id,
    )

    def _status(pct, msg):
        console.print(f"  [{pct:3.0f}%] {msg}")

    try:
        if not stack_name:
            schema, tier, matched = bootstrap_mod.resolve_schema(
                request, status_cb=_status
            )
            if schema is None:
                console.print("[red]✗ Failed to author a schema[/red]")
                sys.exit(1)
            import json as _json

            console.print(f"[green]✓ Schema authored (tier: {tier})[/green]")
            if matched:
                console.print(f"  Catalog match: {matched}")
            console.print(_json.dumps(schema, indent=2))
            return

        from idp_sdk import IDPClient

        client = IDPClient(stack_name=stack_name, region=region)
        config_table = client.discovery._get_config_table(stack_name)
        _os.environ["CONFIGURATION_TABLE_NAME"] = config_table

        from idp_common.config.configuration_manager import ConfigurationManager

        config_manager = ConfigurationManager()
        test_set_bucket = _os.environ.get("TEST_SET_BUCKET")

        result = bootstrap_mod.run_bootstrap(
            request,
            config_manager=config_manager,
            test_set_bucket=test_set_bucket,
            status_cb=_status,
        )

        if not result.success:
            console.print(f"[red]✗ Bootstrap failed: {result.error}[/red]")
            sys.exit(1)

        console.print(f"[green]✓ Config profile: {result.config_version}[/green]")
        console.print(f"  Resolution tier: {result.resolution_tier}")
        if result.catalog_match:
            console.print(f"  Catalog match: {result.catalog_match}")
        if result.test_set_id:
            console.print(
                f"[green]✓ Test set: {result.test_set_id} "
                f"({result.docs_generated} doc(s))[/green]"
            )
        elif not result.generator_available:
            console.print(
                "[yellow]Test set skipped (generator unavailable). "
                "Config is ready; upload your own docs to build a test set.[/yellow]"
            )
        if result.error:
            console.print(f"[yellow]Note: {result.error}[/yellow]")

    except Exception as e:
        logger.error(f"Error in bootstrap: {e}", exc_info=True)
        console.print(f"[red]✗ Error: {e}[/red]")
        sys.exit(1)


def main():
    """Main entry point for the CLI"""
    # Pre-flight check: report the underlying ImportError rather than only
    # "not installed". An `idp_sdk` that imports but lacks `IDPClient` means the
    # wrong (non first-party) package is installed — most likely the squatted
    # PyPI package — and that needs a different fix than "run make setup".
    #
    # This covers a missing idp_sdk too: the tier-2 import above fails either
    # way, so no separate `import idp_sdk` probe is needed.
    if _IMPORT_ERROR is not None:
        print(_SETUP_HELP, file=sys.stderr)
        print(f"Underlying import error: {_IMPORT_ERROR}", file=sys.stderr)
        sys.exit(1)

    # Parse --profile from anywhere in sys.argv before Click processes arguments
    args = sys.argv[1:]  # Skip script name
    profile = None

    # Look for --profile in arguments
    i = 0
    while i < len(args):
        if args[i] == "--profile" and i + 1 < len(args):
            profile = args[i + 1]
            # Remove --profile and its value from sys.argv
            sys.argv.pop(i + 1)  # Remove profile value
            sys.argv.pop(i + 1)  # Remove --profile (index shifts after first pop)
            break
        i += 1

    if profile:
        os.environ["AWS_DEFAULT_PROFILE"] = profile
        console.print(f"[green]Using AWS profile: {profile}[/green]")

    cli()


if __name__ == "__main__":
    main()
