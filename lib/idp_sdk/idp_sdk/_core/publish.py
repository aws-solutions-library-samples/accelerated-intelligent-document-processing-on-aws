#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Create new Cfn artifacts bucket if not already existing
Build artifacts
Upload artifacts to S3 bucket for deployment with CloudFormation
"""

import concurrent.futures
import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import boto3
import yaml
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from .cfn_yaml import load_cfn_template, make_cfn_loader
from .s3_security import apply_enforce_ssl_only

LIB_DEPENDENCY = "./lib/idp_common_pkg/idp_common"
LIB_PKG_PATH = "./lib/idp_common_pkg"

# Files the multi-doc discovery CodeBuild project needs from the source zip in
# order to build its container image. Kept as a constant so the packaging step
# and its test assert on the same list.
MULTI_DOC_DISCOVERY_BUILD_INPUTS = (
    "nested/multi-doc-discovery/Dockerfile",
    "nested/multi-doc-discovery/requirements.txt",
)


class IDPPublisher:
    def __init__(self, verbose=False):
        self.console = Console()
        self.verbose = verbose
        self.bucket_basename = None
        self.prefix = None
        self.region = None
        self.acl = None
        self.bucket = None
        self.prefix_and_version = None
        self.version = None
        self.build_errors = []  # Track build errors for verbose reporting
        self.public_sample_udop_model = ""
        self.public = False
        self.main_template = "idp-main.yaml"
        self.use_container_flag = ""
        self.pattern2_use_containers = True  # Default to containers for Pattern-2

        self.s3_client = None
        self.cf_client = None
        self.sts_client = None
        self._is_lib_changed = False
        self.skip_validation = False
        self.lint_enabled = True
        self.headless = False  # Set by operations layer when --headless is requested
        self.govcloud = False  # Set by operations layer when --govcloud is requested
        self.account_id = None
        self._layer_arns = {}  # Store built layer ARNs for template injection

    def clean_checksums(self):
        """Delete all .checksum files and Lambda layer caches for full rebuild"""
        self.console.print(
            "[yellow]🧹 Cleaning build cache for full rebuild...[/yellow]"
        )

        checksum_paths = [
            ".checksum",  # main
            "lib/.checksum",  # lib
        ]

        # Add nested stack checksum files
        nested_dir = "nested"
        if os.path.exists(nested_dir):
            for item in os.listdir(nested_dir):
                nested_path = os.path.join(nested_dir, item)
                if os.path.isdir(nested_path):
                    checksum_paths.append(f"{nested_path}/.checksum")

        # Add patterns checksum files
        patterns_dir = "patterns"
        if os.path.exists(patterns_dir):
            for item in os.listdir(patterns_dir):
                pattern_path = os.path.join(patterns_dir, item)
                if os.path.isdir(pattern_path):
                    checksum_paths.append(f"{pattern_path}/.checksum")

        deleted_count = 0
        for checksum_path in checksum_paths:
            if os.path.exists(checksum_path):
                os.remove(checksum_path)
                self.console.print(f"[green]  ✓ Deleted {checksum_path}[/green]")
                deleted_count += 1

        # Delete cached Lambda layer zips to force layer rebuilds
        layers_dir = ".aws-sam/layers"
        if os.path.exists(layers_dir):
            layer_zips = [f for f in os.listdir(layers_dir) if f.endswith(".zip")]
            for layer_zip in layer_zips:
                layer_path = os.path.join(layers_dir, layer_zip)
                os.remove(layer_path)
                self.console.print(f"[green]  ✓ Deleted {layer_path}[/green]")
                deleted_count += 1

        if deleted_count == 0:
            self.console.print("[dim]  No cache files found to delete[/dim]")
        else:
            self.console.print(
                f"[green]✅ Deleted {deleted_count} cache files - full rebuild will be triggered[/green]"
            )

    def _find_all_requirements_files(self):
        """Find all requirements.txt files in the project"""
        requirements_files = []

        # Main Lambda functions
        src_lambda_dir = Path("src/lambda")
        if src_lambda_dir.exists():
            for func_dir in src_lambda_dir.iterdir():
                req_file = func_dir / "requirements.txt"
                if req_file.exists():
                    requirements_files.append(str(req_file))

        # Nested Lambda functions
        nested_dir = Path("nested")
        if nested_dir.exists():
            for nested_item in nested_dir.iterdir():
                nested_src = nested_item / "src"
                if nested_src.exists():
                    for func_dir in nested_src.iterdir():
                        req_file = func_dir / "requirements.txt"
                        if req_file.exists():
                            requirements_files.append(str(req_file))

        # Pattern Lambda functions
        patterns_dir = Path("patterns")
        if patterns_dir.exists():
            for pattern_dir in patterns_dir.iterdir():
                pattern_src = pattern_dir / "src"
                if pattern_src.exists():
                    for func_dir in pattern_src.iterdir():
                        req_file = func_dir / "requirements.txt"
                        if req_file.exists():
                            requirements_files.append(str(req_file))

        return requirements_files

    def _prepare_for_build_at_start(self):
        """Run at script startup - placeholder for future startup checks"""
        self.log_verbose("✅ Build startup checks complete")

    def log_verbose(self, message, style="dim"):
        """Log verbose messages if verbose mode is enabled"""
        if self.verbose:
            # Use markup=False to prevent Rich from eating brackets like [extras]
            self.console.print(message, style=style, markup=False)

    # ========================================================================
    # LOGGING HELPERS - Consistent styling for all output
    # ========================================================================

    def log_phase(self, title, emoji=""):
        """Print a major phase header with separators"""
        separator = "═" * 65
        self.console.print(f"\n[bold cyan]{separator}[/bold cyan]")
        if emoji:
            self.console.print(f"[bold cyan] {emoji} {title.upper()}[/bold cyan]")
        else:
            self.console.print(f"[bold cyan] {title.upper()}[/bold cyan]")
        self.console.print(f"[bold cyan]{separator}[/bold cyan]")

    def log_task(self, message, thread=None):
        """Print task start (cyan with arrow)"""
        prefix = f"[{thread}] " if thread else ""
        self.console.print(f"[cyan]▶ {prefix}{message}[/cyan]")

    def log_detail(self, message, thread=None):
        """Print indented detail info (dim)"""
        prefix = f"[{thread}] " if thread else ""
        self.console.print(f"[dim]  └─ {prefix}{message}[/dim]")

    def log_success(self, message, thread=None):
        """Print success message (green checkmark)"""
        prefix = f"[{thread}] " if thread else ""
        self.console.print(f"[green]✓ {prefix}{message}[/green]")

    def log_cached(self, message, thread=None):
        """Print cached/skipped message (blue arrow)"""
        prefix = f"[{thread}] " if thread else ""
        self.console.print(f"[blue]→ {prefix}{message}[/blue]")

    def log_warning(self, message, thread=None):
        """Print warning message (yellow)"""
        prefix = f"[{thread}] " if thread else ""
        self.console.print(f"[yellow]⚠ {prefix}{message}[/yellow]")

    def log_error(self, message, thread=None):
        """Print error message (red X)"""
        prefix = f"[{thread}] " if thread else ""
        self.console.print(f"[red]✗ {prefix}{message}[/red]")

    def upload_to_s3_with_timer(self, local_path, s3_key, description):
        """Upload file to S3 with a spinner, elapsed time display, and optimized transfer config.

        Uses multi-threaded, multipart uploads for better performance on slow connections.
        Shows progress during upload and final timing on completion.
        """
        # Optimized transfer config for better upload performance
        # Matches AWS CLI's optimized defaults for parallel uploads
        transfer_config = TransferConfig(
            multipart_threshold=5
            * 1024
            * 1024,  # 5 MB - enable multipart for smaller files
            max_concurrency=10,  # Use 10 threads for parallel chunk uploads
            multipart_chunksize=5 * 1024 * 1024,  # 5 MB chunks
            use_threads=True,  # Enable multi-threading
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=self.console,
            transient=True,  # Clears spinner after completion
        ) as progress:
            progress.add_task(f"[cyan]Uploading {description}...", total=None)
            start = time.time()
            self.s3_client.upload_file(
                local_path, self.bucket, s3_key, Config=transfer_config
            )
            elapsed = time.time() - start
        self.log_success(f"Uploaded {description} ({elapsed:.1f}s)")

    def log_error_details(self, component, error_output):
        """Log detailed error information and store for summary"""
        error_info = {"component": component, "error": error_output}
        self.build_errors.append(error_info)

        if self.verbose:
            self.console.print(f"[red]❌ {component} build failed:[/red]")
            self.console.print(f"[red]{error_output}[/red]")
        else:
            self.console.print(
                f"[red]❌ {component} build failed (use --verbose for details)[/red]"
            )

    def run_subprocess_with_logging(
        self, cmd, component_name, cwd=None, realtime=False
    ):
        """Run subprocess with standardized logging"""
        if realtime:
            # Real-time output for long-running processes like npm install
            self.console.print(f"[cyan]Running: {' '.join(cmd)}[/cyan]")

            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=cwd,
                    bufsize=1,
                    universal_newlines=True,
                )

                output_lines = []
                while True:
                    output = process.stdout.readline()
                    if output == "" and process.poll() is not None:
                        break
                    if output:
                        line = output.strip()
                        output_lines.append(line)
                        # Show progress for npm commands
                        if "npm" in " ".join(cmd):
                            if any(
                                keyword in line.lower()
                                for keyword in [
                                    "downloading",
                                    "installing",
                                    "added",
                                    "updated",
                                    "audited",
                                ]
                            ):
                                self.console.print(f"[dim]  {line}[/dim]")
                            elif "warn" in line.lower():
                                self.console.print(f"[yellow]  {line}[/yellow]")
                            elif "error" in line.lower():
                                self.console.print(f"[red]  {line}[/red]")

                return_code = process.poll()

                if return_code != 0:
                    error_msg = f"""Command failed: {" ".join(cmd)}
Working directory: {cwd or os.getcwd()}
Return code: {return_code}

OUTPUT:
{chr(10).join(output_lines)}"""
                    print(error_msg)
                    self.log_error_details(component_name, error_msg)
                    return False, error_msg

                return True, None  # Success, no result object needed for real-time

            except Exception as e:
                error_msg = (
                    f"Failed to execute command: {' '.join(cmd)}\nError: {str(e)}"
                )
                self.log_error_details(component_name, error_msg)
                return False, error_msg
        else:
            # Original behavior - capture all output
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
            if result.returncode != 0:
                error_msg = f"""Command failed: {" ".join(cmd)}
Working directory: {cwd or os.getcwd()}
Return code: {result.returncode}

STDOUT:
{result.stdout}

STDERR:
{result.stderr}"""
                print(error_msg)
                self.log_error_details(component_name, error_msg)
                return False, error_msg
            return True, result

    def print_error_summary(self):
        """Print summary of all build errors"""
        if not self.build_errors:
            return

        self.console.print("\n[red]❌ Build Error Summary:[/red]")
        for i, error_info in enumerate(self.build_errors, 1):
            self.console.print(f"\n[red]{i}. {error_info['component']}:[/red]")
            if self.verbose:
                self.console.print(f"[red]{error_info['error']}[/red]")
            else:
                # Show first few lines of error for non-verbose mode
                error_lines = error_info["error"].strip().split("\n")
                preview_lines = error_lines[:3]  # Show first 3 lines
                for line in preview_lines:
                    self.console.print(f"[red]  {line}[/red]")
                if len(error_lines) > 3:
                    self.console.print(
                        f"[dim]  ... ({len(error_lines) - 3} more lines, use --verbose for full output)[/dim]"
                    )

    def print_usage(self):
        """Print usage information with Rich formatting"""
        self.console.print("\n[bold cyan]Usage:[/bold cyan]")
        self.console.print(
            "  python3 publish.py <cfn_bucket_basename> <cfn_prefix> <region> [public] [--max-workers N] [--verbose] [--no-validate] [--lint on|off]"
        )

        self.console.print("\n[bold cyan]Parameters:[/bold cyan]")
        self.console.print(
            "  [yellow]<cfn_bucket_basename>[/yellow]: Base name for the CloudFormation artifacts bucket"
        )
        self.console.print("  [yellow]<cfn_prefix>[/yellow]: S3 prefix for artifacts")
        self.console.print("  [yellow]<region>[/yellow]: AWS region for deployment")
        self.console.print(
            "  [yellow][public][/yellow]: Optional. If 'public', artifacts will be made publicly readable"
        )
        self.console.print(
            "  [yellow][--max-workers N][/yellow]: Optional. Maximum number of concurrent workers (default: auto-detect)"
        )
        self.console.print(
            "                     Use 1 for sequential processing, higher numbers for more concurrency"
        )
        self.console.print(
            "  [yellow][--verbose, -v][/yellow]: Optional. Enable verbose output for debugging"
        )
        self.console.print(
            "  [yellow][--no-validate][/yellow]: Optional. Skip CloudFormation template validation"
        )
        self.console.print(
            "  [yellow][--clean-build][/yellow]: Optional. Delete all .checksum files to force full rebuild"
        )
        self.console.print(
            "  [yellow][--lint on|off][/yellow]: Optional. Enable/disable UI linting and build validation (default: on)"
        )

    def check_parameters(self, args):
        """Check and validate input parameters"""
        if len(args) < 3:
            self.console.print("[red]Error: Missing required parameters[/red]")
            self.print_usage()
            sys.exit(1)

        # Parse arguments
        self.bucket_basename = args[0]
        self.prefix = args[1].rstrip("/")  # Remove trailing slash
        self.region = args[2]

        # Default values
        self.public = False
        self.acl = "bucket-owner-full-control"
        self.max_workers = None  # Auto-detect

        # Parse optional arguments
        remaining_args = args[3:]
        i = 0
        while i < len(remaining_args):
            arg = remaining_args[i]

            if arg.lower() == "public":
                self.public = True
                self.acl = "public-read"
                self.console.print(
                    "[green]Published S3 artifacts will be accessible by public.[/green]"
                )
            elif arg == "--max-workers":
                if i + 1 >= len(remaining_args):
                    self.console.print(
                        "[red]Error: --max-workers requires a number[/red]"
                    )
                    self.print_usage()
                    sys.exit(1)
                try:
                    self.max_workers = int(remaining_args[i + 1])
                    if self.max_workers < 1:
                        self.console.print(
                            "[red]Error: --max-workers must be at least 1[/red]"
                        )
                        sys.exit(1)
                    self.console.print(
                        f"[green]Using {self.max_workers} concurrent workers[/green]"
                    )
                    i += 1  # Skip the next argument (the number)
                except ValueError:
                    self.console.print(
                        "[red]Error: --max-workers must be followed by a valid number[/red]"
                    )
                    self.print_usage()
                    sys.exit(1)
            elif arg in ["--verbose", "-v"]:
                self.verbose = True
                self.console.print("[green]Verbose mode enabled[/green]")
            elif arg == "--no-validate":
                self.skip_validation = True
                self.console.print(
                    "[yellow]CloudFormation template validation will be skipped[/yellow]"
                )
            elif arg == "--lint":
                if i + 1 >= len(remaining_args):
                    self.console.print(
                        "[red]Error: --lint requires 'on' or 'off'[/red]"
                    )
                    self.print_usage()
                    sys.exit(1)
                lint_value = remaining_args[i + 1].lower()
                if lint_value not in ["on", "off"]:
                    self.console.print("[red]Error: --lint must be 'on' or 'off'[/red]")
                    self.print_usage()
                    sys.exit(1)
                self.lint_enabled = lint_value == "on"
                i += 1  # increment arg counter to avoid parsing "on/off" as an arg of its own
            elif arg == "--clean-build":
                self.clean_checksums()
            else:
                self.console.print(
                    f"[yellow]Warning: Unknown argument '{arg}' ignored[/yellow]"
                )

            i += 1

        if not self.public:
            self.console.print(
                "[yellow]Published S3 artifacts will NOT be accessible by public.[/yellow]"
            )

    def setup_environment(self):
        """Set up environment variables and derived values"""
        os.environ["AWS_DEFAULT_REGION"] = self.region

        # Initialize AWS clients
        self.s3_client = boto3.client("s3", region_name=self.region)
        self.cf_client = boto3.client("cloudformation", region_name=self.region)

        # Read version
        try:
            with open("./VERSION", "r") as f:
                self.version = f.read().strip()
        except FileNotFoundError:
            self.console.print("[red]Error: VERSION file not found[/red]")
            sys.exit(1)

        self.prefix_and_version = f"{self.prefix}/{self.version}"
        self.bucket = f"{self.bucket_basename}-{self.region}"

        # Set UDOP model path based on region
        self.public_sample_udop_model = f"s3://aws-ml-blog-{self.region}/artifacts/genai-idp/udop-finetuning/rvl-cdip/model.tar.gz"

    def check_prerequisites(self):
        """Check for required commands and versions"""
        # Check required commands
        required_commands = ["aws", "sam", "uv"]
        for cmd in required_commands:
            if not shutil.which(cmd):
                self.console.print(
                    f"[red]Error: {cmd} is required but not installed[/red]"
                )
                sys.exit(1)

        # Check SAM version
        try:
            result = subprocess.run(
                ["sam", "--version"], capture_output=True, text=True, check=True
            )
            sam_version = result.stdout.split()[3]  # Extract version from output
            min_sam_version = "1.129.0"
            if self.version_compare(sam_version, min_sam_version) < 0:
                self.console.print(
                    f"[red]Error: sam version >= {min_sam_version} is required. (Installed version is {sam_version})[/red]"
                )
                self.console.print(
                    "[yellow]Install: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/manage-sam-cli-versions.html[/yellow]"
                )
                sys.exit(1)
        except subprocess.CalledProcessError:
            self.console.print("[red]Error: Could not determine SAM version[/red]")
            sys.exit(1)

        # Check Python version
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        min_python_version = "3.12"
        if self.version_compare(python_version, min_python_version) < 0:
            self.console.print(
                f"[red]Error: Python version >= {min_python_version} is required. (Installed version is {python_version})[/red]"
            )
            sys.exit(1)

    def version_compare(self, version1, version2):
        """Compare two version strings. Returns -1 if v1 < v2, 0 if equal, 1 if v1 > v2"""

        def normalize(v):
            # Handle nightly/dev builds (e.g. "dev202606120901") — treat as very high version
            parts = []
            for x in v.split("."):
                try:
                    parts.append(int(x))
                except ValueError:
                    # Non-numeric segment (e.g. nightly "dev...") — treat as 999 to pass version check
                    parts.append(999)
            return parts

        v1_parts = normalize(version1)
        v2_parts = normalize(version2)

        # Pad shorter version with zeros
        max_len = max(len(v1_parts), len(v2_parts))
        v1_parts.extend([0] * (max_len - len(v1_parts)))
        v2_parts.extend([0] * (max_len - len(v2_parts)))

        for i in range(max_len):
            if v1_parts[i] < v2_parts[i]:
                return -1
            elif v1_parts[i] > v2_parts[i]:
                return 1
        return 0

    def setup_artifacts_bucket(self):
        """Create bucket if necessary"""
        try:
            self.s3_client.head_bucket(Bucket=self.bucket)
            self.console.print(f"[green]Using existing bucket: {self.bucket}[/green]")
            # Additive hardening on a bucket we didn't create: ensure non-TLS
            # requests are denied. Never fatal — the operator may own the
            # bucket policy and not have granted us s3:PutBucketPolicy.
            if apply_enforce_ssl_only(
                self.s3_client, self.bucket, self.region, raise_on_error=False
            ):
                self.console.print(
                    "[green]EnforceSSLOnly bucket policy in place[/green]"
                )
            else:
                self.console.print(
                    "[yellow]Could not verify/apply the EnforceSSLOnly bucket policy "
                    "on the existing bucket — add it manually.[/yellow]"
                )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "404":
                self.console.print(
                    f"[yellow]Creating s3 bucket: {self.bucket}[/yellow]"
                )
                try:
                    if self.region == "us-east-1":
                        self.s3_client.create_bucket(Bucket=self.bucket)
                    else:
                        self.s3_client.create_bucket(
                            Bucket=self.bucket,
                            CreateBucketConfiguration={
                                "LocationConstraint": self.region
                            },
                        )

                    # Enable versioning
                    self.s3_client.put_bucket_versioning(
                        Bucket=self.bucket,
                        VersioningConfiguration={"Status": "Enabled"},
                    )

                    # Deny any non-TLS request (same EnforceSSLOnly statement
                    # the CloudFormation-managed buckets carry). Fatal here —
                    # we just created the bucket, so we own its policy.
                    apply_enforce_ssl_only(self.s3_client, self.bucket, self.region)
                    self.console.print(
                        "[green]Applied EnforceSSLOnly bucket policy[/green]"
                    )
                except (ClientError, RuntimeError) as create_error:
                    self.console.print(
                        f"[red]Failed to create bucket: {create_error}[/red]"
                    )
                    sys.exit(1)
            else:
                self.console.print("[red]Error accessing bucket:[/red]")
                self.console.print(str(e), style="red", markup=False)
                sys.exit(1)

    def get_file_checksum(self, file_path):
        """Get SHA256 checksum of a file"""
        if not os.path.exists(file_path):
            return ""

        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    def get_directory_checksum(self, directory):
        """Get combined checksum of all files in a directory, excluding development artifacts"""
        if not os.path.exists(directory):
            return ""

        # Define patterns to exclude from checksum calculation
        exclude_dirs = {
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "build",
            "dist",
            ".aws-sam",
            "node_modules",
            ".git",
            ".vscode",
            ".idea",
            "test-reports",  # Exclude test report directories
        }

        exclude_file_patterns = {
            ".checksum",
            ".build_checksum",
            "lib/.checksum",
            ".pyc",
            ".pyo",
            ".pyd",
            ".so",
            ".egg-info",
            ".coverage",
            ".DS_Store",
            "Thumbs.db",
            "coverage.xml",  # Coverage reports
            "test-results.xml",  # Test result reports
            ".gitkeep",  # Git placeholder files
        }

        exclude_file_suffixes = (
            ".pyc",
            ".pyo",
            ".pyd",
            ".so",
            ".coverage",
            ".log",  # Log files
        )
        exclude_dir_suffixes = (".egg-info",)

        def should_exclude_dir(dir_name):
            """Check if directory should be excluded from checksum"""
            if dir_name in exclude_dirs:
                return True
            if any(dir_name.endswith(suffix) for suffix in exclude_dir_suffixes):
                return True
            # Exclude test directories for library checksum only
            if "lib" in directory and (
                dir_name == "tests" or dir_name.startswith("test_")
            ):
                return True
            return False

        def should_exclude_file(file_name):
            """Check if file should be excluded from checksum"""
            if file_name in exclude_file_patterns:
                return True
            if any(file_name.endswith(suffix) for suffix in exclude_file_suffixes):
                return True
            # Exclude test files for library checksum only
            if "lib" in directory and (
                file_name.startswith("test_")
                or file_name.endswith("_test.py")
                or file_name == "nodeids"  # pytest cache files
                or file_name == "lastfailed"  # pytest cache files
                or file_name
                in ["coverage.xml", "test-results.xml"]  # specific test report files
            ):
                return True
            return False

        checksums = []
        for root, dirs, files in os.walk(directory):
            # Filter out excluded directories in-place to prevent os.walk from descending into them
            dirs[:] = [d for d in dirs if not should_exclude_dir(d)]

            # Sort to ensure consistent ordering
            dirs.sort()
            files.sort()

            for file in files:
                if not should_exclude_file(file):
                    file_path = os.path.join(root, file)
                    if os.path.isfile(file_path):
                        checksums.append(self.get_file_checksum(file_path))

        # Combine all checksums
        combined = "".join(checksums)
        return hashlib.sha256(combined.encode()).hexdigest()

    def build_and_package_template(self, directory, force_rebuild=False):
        """Build and package a template directory with smart rebuild detection"""
        # Track build time
        build_start = time.time()

        try:
            # Pattern-2 uses containers - images built separately by build_and_push_pattern2_containers()
            # SAM build with SkipBuild: True just prepares template
            cmd = ["sam", "build", "--template-file", "template.yaml"]

            # Add container flag if needed
            if self.use_container_flag and self.use_container_flag.strip():
                cmd.append(self.use_container_flag)

            if self.verbose:
                cmd.append("--debug")

            sam_build_start = time.time()

            # Validate Python syntax before building
            if not self._validate_python_syntax(directory):
                raise Exception("Python syntax validation failed")

            self.log_verbose(
                f"Running SAM build command in {directory}: {' '.join(cmd)}"
            )
            # Run SAM build from the pattern directory
            success, result = self.run_subprocess_with_logging(
                cmd, f"SAM build for {directory}", directory
            )
            sam_build_time = time.time() - sam_build_start

            if not success:
                raise Exception("SAM build failed")

            # Package the template (using absolute paths)
            build_template_path = os.path.join(
                directory, ".aws-sam", "build", "template.yaml"
            )
            # Use standard packaged.yaml name
            packaged_template_path = os.path.join(
                directory, ".aws-sam", "packaged.yaml"
            )

            cmd = [
                "sam",
                "package",
                "--template-file",
                build_template_path,
                "--output-template-file",
                packaged_template_path,
                "--s3-bucket",
                self.bucket,
                "--s3-prefix",
                self.prefix_and_version,
            ]
            if self.verbose:
                cmd.append("--debug")

            # Patterns with container images need --image-repository even with SkipBuild: True
            # SAM package uses this to generate correct ImageUri references in the template
            if directory in [
                "patterns/unified",
                "nested/multi-doc-discovery",
            ]:
                placeholder_ecr = (
                    f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com/placeholder"
                )
                cmd.extend(["--image-repository", placeholder_ecr])

            sam_package_start = time.time()
            self.log_verbose(f"Running SAM package command: {' '.join(cmd)}")
            # Run SAM package from project root (no cwd change needed)
            success, result = self.run_subprocess_with_logging(
                cmd, f"SAM package for {directory}"
            )
            sam_package_time = time.time() - sam_package_start

            if not success:
                raise Exception("SAM package failed")

            # Log S3 upload location for Lambda artifacts
            self.console.print(
                f"[dim]  📤 Lambda artifacts uploaded to s3://{self.bucket}/{self.prefix_and_version}/[/dim]"
            )

            # Log timing information
            total_time = time.time() - build_start
            pattern_name = os.path.basename(directory)
            self.console.print(
                f"[dim]  {pattern_name}: build={sam_build_time:.1f}s, package={sam_package_time:.1f}s, total={total_time:.1f}s[/dim]"
            )

        except Exception as e:
            # Delete checksum on any failure to force rebuild next time
            self._delete_checksum_file(directory)
            self.log_verbose(f"Exception in build_and_package_template: {str(e)}")
            self.log_verbose(f"Traceback: {traceback.format_exc()}")
            self.console.print(f"[red]❌ Build failed for {directory}:[/red]")
            self.console.print(str(e), style="red", markup=False)
            sys.exit(1)

        return True

    def build_components_with_smart_detection(
        self, components_needing_rebuild, component_type, max_workers=None
    ):
        """Build patterns or options with smart detection using Lambda Layers."""
        # Filter components by type
        components_to_build = []
        for item in components_needing_rebuild:
            if component_type in item["component"]:
                components_to_build.append(item["component"])

        if not components_to_build:
            self.console.print(f"[green]✅ All {component_type} are up to date[/green]")
            return True

        self.console.print(
            f"[cyan]Building {len(components_to_build)} {component_type} with {max_workers} workers...[/cyan]"
        )

        return self._build_components_concurrently(
            components_to_build, component_type, max_workers
        )

    def _build_components_concurrently(self, components, component_type, max_workers):
        """Generic method to build components concurrently with simple logging.

        Note: Progress bars removed to avoid Rich LiveDisplay conflicts when building
        categories concurrently. Simple status logging used instead.
        """
        # Use ThreadPoolExecutor for I/O bound operations (sam build/package)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all component build tasks
            future_to_component = {}
            for component in components:
                self.log_task("Building...", thread=component)
                future = executor.submit(
                    self.build_and_package_template, component, force_rebuild=True
                )
                future_to_component[future] = component

            # Wait for all tasks to complete and check results
            all_successful = True
            completed = 0

            for future in concurrent.futures.as_completed(future_to_component):
                component = future_to_component[future]
                completed += 1

                try:
                    success = future.result()
                    if not success:
                        self.log_error("Build failed!", thread=component)
                        all_successful = False
                    else:
                        self.log_success(
                            f"Complete ({completed}/{len(components)})",
                            thread=component,
                        )

                except Exception as e:
                    # Log detailed error information
                    error_output = (
                        f"Exception: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
                    )
                    self.log_error_details(
                        f"{component_type.title()} {component} build exception",
                        error_output,
                    )
                    self.log_error(f"Error: {str(e)[:50]}...", thread=component)
                    all_successful = False

        return all_successful

    def generate_config_file_list(self):
        """Generate list of configuration files for explicit copying"""
        config_dir = "config_library"
        file_list = []

        for root, dirs, files in os.walk(config_dir):
            for file in files:
                file_path = os.path.join(root, file)
                relative_path = os.path.relpath(file_path, config_dir)
                file_list.append(relative_path)

        return sorted(file_list)

    def _extract_function_name(self, dir_name, template_path):
        """Extract CloudFormation function name from template by matching CodeUri."""
        try:
            # This only needs to read CodeUri, so an intrinsic collapses to its
            # raw scalar: `CodeUri: !Sub '${X}/src'` yields '${X}/src', whose
            # last path segment is still the directory name being matched.
            #
            # This replaces a hand-maintained list of 16 intrinsic tags. Any tag
            # outside that list used to raise, get swallowed by the except below,
            # and silently cost this function its answer — a template using, say,
            # `!Transform` anywhere would resolve no function names at all.
            def _raw_scalar(loader, tag_suffix, node):
                if isinstance(node, yaml.ScalarNode):
                    return loader.construct_scalar(node)
                if isinstance(node, yaml.SequenceNode):
                    return loader.construct_sequence(node, deep=True)
                if isinstance(node, yaml.MappingNode):
                    return loader.construct_mapping(node, deep=True)
                return None

            # Safe: cfn_yaml builds a yaml.SafeLoader subclass, so no
            # Python-object construction is possible, and _raw_scalar returns
            # only plain scalars/sequences/mappings. See that module's docstring.
            template = load_cfn_template(template_path, make_cfn_loader(_raw_scalar))

            if not template or not isinstance(template, dict):
                raise Exception(f"Failed to parse YAML template: {template_path}")

            resources = template.get("Resources", {})
            for resource_name, resource_config in resources.items():
                if (
                    resource_config
                    and isinstance(resource_config, dict)
                    and resource_config.get("Type") == "AWS::Serverless::Function"
                ):
                    properties = resource_config.get("Properties", {})
                    if properties and isinstance(properties, dict):
                        code_uri = properties.get("CodeUri", "")
                        if isinstance(code_uri, str):
                            code_uri = code_uri.rstrip("/")
                            code_dir = (
                                code_uri.split("/")[-1] if "/" in code_uri else code_uri
                            )
                            if code_dir == dir_name:
                                return resource_name
            raise Exception(
                f"No CloudFormation function found for directory {dir_name} in template {template_path}"
            )

        except Exception as e:
            self.console.print(
                f"[yellow]⚠ Warning: Could not extract function name for {dir_name} from {template_path}:[/yellow]"
            )
            self.console.print(f"[dim]{str(e)}[/dim]")
            # Don't exit - just skip this function
            return None

    def upload_config_library(self):
        """Upload configuration library to S3 using aws s3 sync.

        Uses AWS CLI's built-in concurrency and delta sync for optimal performance.
        AWS CLI automatically skips unchanged files and uses parallel uploads.
        """
        self.log_phase("Uploading Config Library", "📂")
        config_dir = "config_library"

        if not os.path.exists(config_dir):
            self.log_warning(f"{config_dir} directory not found")
            return

        # Count files for reporting
        file_count = sum(len(files) for _, _, files in os.walk(config_dir))
        s3_dest = f"s3://{self.bucket}/{self.prefix_and_version}/config_library"

        self.log_task(f"Syncing {file_count} config files to S3...")

        # Use aws s3 sync with progress spinner and timing
        cmd = [
            "aws",
            "s3",
            "sync",
            config_dir,
            s3_dest,
            "--region",
            self.region,
        ]

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=self.console,
            transient=True,
        ) as progress:
            progress.add_task("[cyan]Syncing config library to S3...", total=None)
            start = time.time()

            result = subprocess.run(cmd, capture_output=True, text=True)
            elapsed = time.time() - start

        if result.returncode != 0:
            self.log_error(f"Failed to sync config library: {result.stderr}")
            sys.exit(1)

        self.log_success(
            f"Config library synced ({file_count} files in {elapsed:.1f}s)"
        )

    def ui_changed(self):
        """Check if UI has changed based on zipfile hash, returns (changed, zipfile_path)"""
        ui_hash = self.compute_ui_hash()
        zipfile_name = f"src-{ui_hash[:16]}.zip"
        zipfile_path = os.path.join(".aws-sam", zipfile_name)

        existing_zipfiles = (
            [
                f
                for f in os.listdir(".aws-sam")
                if f.startswith("src-") and f.endswith(".zip")
            ]
            if os.path.exists(".aws-sam")
            else []
        )

        if zipfile_name not in existing_zipfiles:
            # Remove old zipfiles
            for old_zip in existing_zipfiles:
                old_path = os.path.join(".aws-sam", old_zip)
                if os.path.exists(old_path):
                    os.remove(old_path)
            return True, zipfile_path

        return not os.path.exists(zipfile_path), zipfile_path

    def start_ui_validation_parallel(self):
        """Start UI validation in parallel if needed, returns (future, executor)"""
        if not self.lint_enabled or not os.path.exists("src/ui"):
            return None, None

        changed, _ = self.ui_changed()
        if not changed:
            return None, None

        ui_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        ui_validation_future = ui_executor.submit(self.validate_ui_build)
        self.console.print(
            "[cyan]🔍 Starting UI validation in parallel with builds...[/cyan]"
        )
        return ui_validation_future, ui_executor

    def compute_ui_hash(self):
        """Compute hash of UI folder contents"""
        self.console.print("[cyan]Computing hash of ui folder contents[/cyan]")
        ui_dir = "src/ui"
        return self.get_directory_checksum(ui_dir)

    def validate_ui_build(self):
        """Validate UI build to catch ESLint/Prettier errors before packaging"""
        try:
            self.console.print("[bold cyan]🔍 VALIDATING UI build[/bold cyan]")
            ui_dir = "src/ui"

            if not os.path.exists(ui_dir):
                self.console.print(
                    "[yellow]No UI directory found, skipping UI validation[/yellow]"
                )
                return

            # Run npm ci first (clean install from lock file)
            self.console.print(
                "[cyan]📦 Installing UI dependencies (this may take a while)...[/cyan]"
            )
            success, result = self.run_subprocess_with_logging(
                ["npm", "ci"], "UI npm ci", ui_dir, realtime=True
            )

            if not success:
                raise Exception("npm ci failed")

            # Run npm run build to validate ESLint/Prettier
            self.console.print(
                "[cyan]🔨 Building UI (validating ESLint/Prettier)...[/cyan]"
            )
            success, result = self.run_subprocess_with_logging(
                ["npm", "run", "build"], "UI build validation", ui_dir, realtime=True
            )

            if not success:
                raise Exception("UI build validation failed")

            self.console.print("[green]✅ UI build validation passed[/green]")

        except Exception as e:
            self.console.print("[red]❌ UI build validation failed:[/red]")
            self.console.print(str(e), style="red", markup=False)
            sys.exit(1)

    def package_ui(self):
        """Package UI source code"""
        _, zipfile_path = self.ui_changed()

        if not os.path.exists(zipfile_path):
            os.makedirs(".aws-sam", exist_ok=True)
            with zipfile.ZipFile(zipfile_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                ui_dir = "src/ui"
                exclude_dirs = {"node_modules", "build", ".aws-sam"}
                for root, dirs, files in os.walk(ui_dir):
                    dirs[:] = [d for d in dirs if d not in exclude_dirs]
                    for file in files:
                        if file == ".env" or file.startswith(".env."):
                            continue
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, ui_dir)
                        zipf.write(file_path, arcname)

        # Check if file exists in S3 and upload if needed
        zipfile_name = os.path.basename(zipfile_path)
        s3_key = f"{self.prefix_and_version}/{zipfile_name}"
        try:
            self.s3_client.head_object(Bucket=self.bucket, Key=s3_key)
            self.console.print(
                f"[green]WebUI zipfile already exists in S3: {zipfile_name}[/green]"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                self.console.print("[cyan]Upload source to S3[/cyan]")
                try:
                    self.s3_client.upload_file(zipfile_path, self.bucket, s3_key)
                    self.console.print(
                        f"[green]Uploaded WebUI zipfile to S3: {zipfile_name}[/green]"
                    )
                except ClientError as upload_error:
                    self.console.print(
                        f"[red]Error uploading UI zipfile: {upload_error}[/red]"
                    )
                    sys.exit(1)
            else:
                self.console.print("[red]Error checking S3 for UI zipfile:[/red]")
                self.console.print(str(e), style="red", markup=False)
                sys.exit(1)

        return zipfile_name

    def package_unified_source(self):
        """Package unified pattern source code for CodeBuild to build all Docker images"""
        self.console.print(
            "[bold cyan]📦 Packaging unified pattern source for Docker builds[/bold cyan]"
        )

        # Calculate content hash for versioning
        paths_to_hash = [
            "Dockerfile.optimized",
            "patterns/unified/buildspec.yml",
            "lib/idp_common_pkg",
            "patterns/unified/src",
        ]

        combined_hash = hashlib.sha256()
        for path in paths_to_hash:
            if os.path.isfile(path):
                file_hash = self.get_file_checksum(path)
                if file_hash:
                    combined_hash.update(file_hash.encode())
            elif os.path.isdir(path):
                dir_hash = self.get_component_checksum(path)
                if dir_hash:
                    combined_hash.update(dir_hash.encode())

        content_hash = combined_hash.hexdigest()[:8]
        zipfile_name = f"unified-source-{content_hash}.zip"
        zipfile_path = os.path.join(".aws-sam", zipfile_name)

        # Create zip if it doesn't exist
        if not os.path.exists(zipfile_path):
            os.makedirs(".aws-sam", exist_ok=True)
            self.console.print(
                f"[cyan]Creating unified source zip: {zipfile_name}[/cyan]"
            )

            with zipfile.ZipFile(zipfile_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                # Add Dockerfile
                zipf.write("Dockerfile.optimized", "Dockerfile.optimized")

                # Add unified buildspec
                zipf.write(
                    "patterns/unified/buildspec.yml",
                    "patterns/unified/buildspec.yml",
                )

                # Add lib/idp_common_pkg
                for root, dirs, files in os.walk("lib/idp_common_pkg"):
                    dirs[:] = [
                        d
                        for d in dirs
                        if d
                        not in {
                            "__pycache__",
                            ".pytest_cache",
                            "dist",
                            "build",
                            "*.egg-info",
                        }
                    ]
                    for file in files:
                        if file.endswith((".pyc", ".pyo")):
                            continue
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, ".")
                        zipf.write(file_path, arcname)

                # Add patterns/unified/src (all 12 function directories)
                for root, dirs, files in os.walk("patterns/unified/src"):
                    dirs[:] = [
                        d
                        for d in dirs
                        if d not in {"__pycache__", ".pytest_cache", ".aws-sam"}
                    ]
                    for file in files:
                        if file.endswith((".pyc", ".pyo")):
                            continue
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, ".")
                        zipf.write(file_path, arcname)

            self.console.print(
                f"[green]✅ Created unified source zip ({os.path.getsize(zipfile_path) / 1024 / 1024:.2f} MB)[/green]"
            )

        # Upload to S3 if needed
        s3_key = f"{self.prefix_and_version}/{zipfile_name}"
        try:
            self.s3_client.head_object(Bucket=self.bucket, Key=s3_key)
            self.console.print(
                f"[green]Unified source already exists in S3: {zipfile_name}[/green]"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                self.console.print(
                    f"[cyan]Uploading unified source to S3: {s3_key}[/cyan]"
                )
                try:
                    self.s3_client.upload_file(zipfile_path, self.bucket, s3_key)
                    self.console.print(
                        "[green]✅ Uploaded unified source to S3[/green]"
                    )
                except ClientError as upload_error:
                    self.console.print(
                        f"[red]❌ Error uploading unified source: {upload_error}[/red]"
                    )
                    sys.exit(1)
            else:
                self.console.print(
                    f"[red]❌ Error checking S3 for unified source: {e}[/red]"
                )
                sys.exit(1)

        return zipfile_name

    def package_multi_doc_discovery_source(self):
        """Package multi-doc discovery source code for CodeBuild to build Docker image.

        CodeBuild downloads this zip and uses the inline buildspec to create a Docker
        image containing lib/idp_common_pkg and src/lambda/multi_doc_discovery/*.py.
        """
        self.console.print(
            "[bold cyan]📦 Packaging multi-doc discovery source for Docker builds[/bold cyan]"
        )

        zipfile_name = "multi-doc-discovery-source.zip"
        zipfile_path = os.path.join(".aws-sam", zipfile_name)

        # Always recreate to ensure latest code is included
        os.makedirs(".aws-sam", exist_ok=True)
        self.console.print(
            f"[cyan]Creating multi-doc discovery source zip: {zipfile_name}[/cyan]"
        )

        with zipfile.ZipFile(zipfile_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            # Add lib/idp_common_pkg
            for root, dirs, files in os.walk("lib/idp_common_pkg"):
                dirs[:] = [
                    d
                    for d in dirs
                    if d
                    not in {
                        "__pycache__",
                        ".pytest_cache",
                        "dist",
                        "build",
                        "*.egg-info",
                    }
                ]
                for file in files:
                    if file.endswith((".pyc", ".pyo")):
                        continue
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, ".")
                    zipf.write(file_path, arcname)

            # Add src/lambda/multi_doc_discovery/*.py
            for root, dirs, files in os.walk("src/lambda/multi_doc_discovery"):
                dirs[:] = [d for d in dirs if d not in {"__pycache__", ".pytest_cache"}]
                for file in files:
                    if file.endswith((".pyc", ".pyo")):
                        continue
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, ".")
                    zipf.write(file_path, arcname)

            # Add the build inputs themselves. The CodeBuild buildspec builds
            # `-f nested/multi-doc-discovery/Dockerfile`, which in turn COPYs
            # nested/multi-doc-discovery/requirements.txt, so BOTH must be in
            # the zip. They used to be heredoc'd inline in the buildspec, which
            # let the real files (and Dependabot's security bumps to them) drift
            # out of the image entirely.
            for build_input in MULTI_DOC_DISCOVERY_BUILD_INPUTS:
                if not os.path.isfile(build_input):
                    self.console.print(
                        f"[red]❌ Missing multi-doc discovery build input: "
                        f"{build_input}[/red]"
                    )
                    sys.exit(1)
                zipf.write(build_input, build_input)

        self.console.print(
            f"[green]✅ Created multi-doc discovery source zip ({os.path.getsize(zipfile_path) / 1024 / 1024:.2f} MB)[/green]"
        )

        # Upload to S3
        s3_key = f"{self.prefix_and_version}/{zipfile_name}"
        try:
            self.s3_client.head_object(Bucket=self.bucket, Key=s3_key)
            self.console.print(
                f"[green]Multi-doc discovery source already in S3: {zipfile_name}[/green]"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                self.console.print(
                    f"[cyan]Uploading multi-doc discovery source to S3: {s3_key}[/cyan]"
                )
                try:
                    self.s3_client.upload_file(zipfile_path, self.bucket, s3_key)
                    self.console.print(
                        "[green]✅ Uploaded multi-doc discovery source to S3[/green]"
                    )
                except ClientError as upload_error:
                    self.console.print(
                        f"[red]❌ Error uploading multi-doc discovery source: {upload_error}[/red]"
                    )
                    sys.exit(1)
            else:
                self.console.print(
                    f"[red]❌ Error checking S3 for multi-doc discovery source: {e}[/red]"
                )
                sys.exit(1)

        return zipfile_name

    def _upload_template_to_s3(self, template_path, s3_key, description):
        """Helper method to upload template to S3 with error handling"""
        self.console.print(f"[cyan]Uploading {description} to S3: {s3_key}[/cyan]")
        try:
            self.s3_client.upload_file(template_path, self.bucket, s3_key)
            self.console.print(f"[green]✅ {description} uploaded successfully[/green]")
        except Exception as e:
            self.console.print(f"[red]Failed to upload {description}:[/red]")
            self.console.print(str(e), style="red", markup=False)
            sys.exit(1)

    def _upload_version_pointer(self):
        """Write `<prefix>/idp-main-latest.json` to the public artifacts bucket.

        A single small pointer object the Web UI's getLatestPublishedVersion
        resolver reads (GetObject of one known key — no ListObjectsV2, so it
        works against the public release bucket). Overwritten on every publish;
        lives at the version-stripped prefix so it always names the newest
        release. `templateUrl` points at the versioned main template.
        """
        basename = self.main_template.replace(".yaml", "")  # e.g. "idp-main"
        pointer_key = f"{self.prefix}/{basename}-latest.json"
        versioned_key = f"{self.prefix}/{basename}_{self.version}.yaml"
        body = json.dumps(
            {
                "version": self.version,
                "templateUrl": (
                    f"https://s3.{self.region}.amazonaws.com/{self.bucket}/{versioned_key}"
                ),
            },
            indent=2,
        ).encode("utf-8")
        try:
            self.s3_client.put_object(
                Bucket=self.bucket,
                Key=pointer_key,
                Body=body,
                ContentType="application/json",
            )
            self.console.print(
                f"[green]✅ Version pointer updated: s3://{self.bucket}/{pointer_key} "
                f"→ {self.version}[/green]"
            )
        except Exception as e:  # noqa: BLE001 — non-fatal; the indicator is optional
            self.console.print(
                f"[yellow]⚠️  Failed to write version pointer (the 'update "
                f"available' indicator may not work): {e}[/yellow]"
            )

    def _check_and_upload_template(self, template_path, s3_key, description):
        """Helper method to check if template exists in S3 and upload if missing"""
        try:
            self.s3_client.head_object(Bucket=self.bucket, Key=s3_key)
            self.console.print(f"[green]✅ {description} already exists in S3[/green]")
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                self.console.print(
                    f"[yellow]{description} missing from S3, uploading: {s3_key}[/yellow]"
                )
                if not os.path.exists(template_path):
                    self.console.print(
                        f"[red]Error: No template to upload at {template_path}[/red]"
                    )
                    sys.exit(1)
                self._upload_template_to_s3(template_path, s3_key, description)
            else:
                self.console.print(
                    f"[yellow]Could not check {description} existence:[/yellow]"
                )
                self.console.print(str(e), style="red", markup=False)

    # Directories under feature-platform/ that contain bundled features to
    # publish automatically during deploy so a fresh stack has a working
    # feature in its catalog without a manual `idp-feature-cli publish`.
    # Public builds bundle only the reference sample; proprietary features
    # live in the separate idp-extensions repo and are published there.
    #
    # The list of OSS feature directories to bundle is declared in
    # config_library/extensions-oss.yaml (the OSS counterpart of
    # extensions-marketplace.yaml). _DEFAULT_BUNDLED_FEATURE_DIRS is the
    # fallback used only when that file is absent (e.g. a trimmed checkout).
    _OSS_EXTENSIONS_FILE = "config_library/extensions-oss.yaml"
    _DEFAULT_BUNDLED_FEATURE_DIRS = [
        "feature-platform/sample-feature",
    ]

    def _bundled_feature_dirs(self):
        """Return the OSS feature directories to bundle, from extensions-oss.yaml.

        The file shares the shape of extensions-marketplace.yaml:
            schemaVersion: "1.0"
            features:
              - path: feature-platform/sample-feature

        Falls back to _DEFAULT_BUNDLED_FEATURE_DIRS when the file is absent. A
        malformed file is a hard error — a broken list would silently drop
        bundled features from the catalog.
        """
        path = Path(self._OSS_EXTENSIONS_FILE)
        if not path.is_file():
            self.log_verbose(
                f"{self._OSS_EXTENSIONS_FILE} not found — using default bundled "
                f"feature list ({self._DEFAULT_BUNDLED_FEATURE_DIRS})."
            )
            return list(self._DEFAULT_BUNDLED_FEATURE_DIRS)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self.log_error(f"Malformed {self._OSS_EXTENSIONS_FILE}: {exc}")
            sys.exit(1)
        entries = data.get("features") or []
        if not isinstance(entries, list):
            self.log_error(f"{self._OSS_EXTENSIONS_FILE} 'features' must be a list.")
            sys.exit(1)
        dirs = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("path"):
                self.log_error(
                    f"{self._OSS_EXTENSIONS_FILE} each feature must be a mapping "
                    f"with a 'path' key; got {entry!r}"
                )
                sys.exit(1)
            dirs.append(entry["path"])
        return dirs

    def _get_feature_deps(self, feature_dir):
        """Return the list of source paths a feature's build depends on."""
        return [
            str(feature_dir / "feature.yaml"),
            str(feature_dir / "template.yaml"),
            str(feature_dir / "feature-api"),
            str(feature_dir / "feature-ui" / "src"),
            str(feature_dir / "feature-ui" / "package.json"),
            str(feature_dir / "feature-ui" / "vite.config.ts"),
            str(feature_dir / "feature-ui" / "tsconfig.json"),
            str(feature_dir / "feature-ui" / "index.html"),
            str(feature_dir / "ui-deployer"),
        ]

    def build_and_upload_sample_features(self):
        """Build and upload bundled sample feature(s) to the artifact bucket.

        Produces artifacts under a VERSION-FREE extension base:
            s3://<artifact-bucket>/<prefix>/extensions/<id>/template.yaml
            s3://<artifact-bucket>/<prefix>/extensions/<id>/<version>/...

        OSS extensions install directly from the artifacts bucket (no host-side
        copy): the generated catalog.json carries each feature's artifactBucket
        + (version-free) artifactPrefix, and getFeatureLaunchUrl builds the
        Launch Stack URL from those. The feature's ui-deployer then copies the
        UI bundle into the host's WebUIBucket at install/update time.

        Returns ``(hash, file_list, oss_catalog_entries)`` — empty when no
        bundled feature directories are present (e.g. a trimmed checkout).
        """
        feature_dirs = [
            Path(d) for d in self._bundled_feature_dirs() if Path(d).is_dir()
        ]
        if not feature_dirs:
            self.log_verbose(
                "No bundled feature directories found — skipping sample-feature "
                "publish (feature bucket will start empty)."
            )
            return "", [], []

        self.log_phase("Building Sample Features", "🧩")

        # Lazy import — idp_feature_sdk ships in lib/idp_feature_sdk/ but some
        # CI envs strip feature trees before this runs.
        try:
            from idp_feature_sdk.manifest import load_manifest
            from idp_feature_sdk.publisher import FeaturePublisher
        except ImportError as exc:
            raise RuntimeError(
                "idp_feature_sdk is required to build bundled features but is not "
                "installed. Install it with: pip install -e lib/idp_feature_sdk/\n"
                f"Original error: {exc}"
            )

        _PUBLISH_FORMAT_VERSION = "v3-multi-feature"
        all_file_lists = []
        all_checksums = []
        oss_catalog_entries = []

        for feature_dir in feature_dirs:
            self.log_task(f"Processing bundled feature: {feature_dir.name}")
            file_list, catalog_entry = self._build_and_upload_single_feature(
                feature_dir, load_manifest, FeaturePublisher
            )
            all_file_lists.extend(file_list)
            if catalog_entry:
                oss_catalog_entries.append(catalog_entry)

            deps = self._get_feature_deps(feature_dir)
            per_feature_checksum = hashlib.sha256(
                "".join(
                    self.get_file_checksum(d)
                    if os.path.isfile(d)
                    else self.get_source_files_checksum(d)
                    for d in deps
                    if os.path.exists(d)
                ).encode()
            ).hexdigest()
            all_checksums.append(per_feature_checksum)

        combined_checksum = hashlib.sha256(
            (_PUBLISH_FORMAT_VERSION + "".join(all_checksums)).encode()
        ).hexdigest()
        return combined_checksum[:16], all_file_lists, oss_catalog_entries

    # Checked-in curated list of closed-source (Marketplace) extensions.
    _MARKETPLACE_EXTENSIONS_FILE = "config_library/extensions-marketplace.yaml"
    # Written into config_library/ so the existing ConfigurationCopyFunction
    # copies it into the stack's ConfigurationBucket at deploy time; the host's
    # listCatalogFeatures resolver reads it from there at runtime (GetObject,
    # no ListObjectsV2, no post-deploy artifacts-bucket dependency).
    _CATALOG_OUTPUT_FILE = "config_library/catalog.json"

    # Self-updating sample-document manifest. Generated at publish time by
    # scanning samples/, written into config_library/ so the existing
    # ConfigurationCopyFunction copies it into the stack's ConfigurationBucket;
    # the Quick Start agent's list_sample_documents tool reads it from there
    # (GetObject, no bucket listing). Mirrors the catalog.json mechanism.
    _SAMPLES_MANIFEST_FILE = "config_library/samples-manifest.json"
    _SAMPLES_DIR = "samples"
    # Document extensions that make sense as Discovery input.
    _SAMPLE_DOC_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".webp")
    # Subdirectories of samples/ that hold batches of documents (indexed as a
    # single "batch" entry). Other subdirs (code samples, hooks) are skipped.
    _SAMPLE_DOC_SUBDIRS = ("w2", "rule-validation")
    # Directory holding the unified-pattern config-library presets. Used to
    # resolve a sample's associated config by folder-name convention when no
    # explicit configId override is provided.
    _UNIFIED_CONFIG_DIR = "config_library/unified"

    # Curated names/descriptions/config-associations for well-known samples;
    # anything not listed falls back to a filename-derived name + generic
    # description (and a folder-name-convention config lookup), so newly added
    # samples are still indexed automatically. Each value is
    # (name, description, configId) where configId is the config_library/unified
    # preset folder the sample is designed for, or None if unassociated.
    _SAMPLE_OVERRIDES = {
        "lending_package.pdf": (
            "Lending Package",
            "Multi-page mortgage loan packet (application, pay stubs, W-2, bank "
            "statements) — a multi-class packet for testing classification + extraction.",
            "lending-package-sample",
        ),
        "lending_package-long.pdf": (
            "Lending Package (long)",
            "Longer multi-page mortgage loan packet variant.",
            "lending-package-sample",
        ),
        "insurance_package.pdf": (
            "Insurance Package",
            "Multi-section insurance claim packet for multi-class processing.",
            None,
        ),
        "insurance_package_single.pdf": (
            "Insurance Package (single)",
            "Single-section insurance document sample.",
            None,
        ),
        "bank-statement-multipage.pdf": (
            "Bank Statement (multi-page)",
            "Multi-page bank statement with transaction tables — good for agentic "
            "table extraction.",
            "bank-statement-sample",
        ),
        "healthcare-multisection-package.pdf": (
            "Healthcare Package",
            "Multi-section healthcare document packet.",
            "healthcare-multisection-package",
        ),
        "rvl_cdip_package.pdf": (
            "RVL-CDIP Package",
            "Mixed-document packet from the RVL-CDIP set for classification testing.",
            "rvl-cdip-package-sample",
        ),
        "DS11-USPassportApplication.pdf": (
            "US Passport Application (DS-11)",
            "US passport application form.",
            "ds11-passport-application",
        ),
        "old_cal_license.png": (
            "California Driver License",
            "Driver license image sample.",
            None,
        ),
        "w2": (
            "W-2 Forms",
            "Batch of W-2 tax form documents.",
            "fake-w2",
        ),
        "rule-validation": (
            "Rule Validation Samples",
            "Documents for the rule-validation pipeline.",
            "rule-validation",
        ),
    }

    def _sample_config_id(self, override_config_id, sample_id):
        """Resolve the config_library preset associated with a sample.

        Prefers the explicit override; otherwise falls back to a folder-name
        convention (config_library/unified/<sample_id> exists). Returns None
        when no association can be established.
        """
        if override_config_id:
            return override_config_id
        candidate = Path(self._UNIFIED_CONFIG_DIR) / sample_id
        if candidate.is_dir():
            return sample_id
        return None

    def _sample_label(self, key):
        """(name, description, configId) for a sample key.

        Uses the curated overrides table when present; otherwise derives a
        name from the filename and attempts a folder-name-convention config
        lookup. ``configId`` is the associated config_library/unified preset,
        or None.
        """
        sample_id = os.path.splitext(os.path.basename(key))[0]
        override = self._SAMPLE_OVERRIDES.get(key)
        if override:
            name, desc, override_config_id = override
            return name, desc, self._sample_config_id(override_config_id, sample_id)
        name = sample_id.replace("_", " ").replace("-", " ").strip().title()
        return (
            name,
            f"Sample document: {name}.",
            self._sample_config_id(None, sample_id),
        )

    def generate_samples_manifest(self):
        """Scan samples/ and write config_library/samples-manifest.json.

        Self-updating: indexes top-level sample documents plus the known
        document subdirectories (as single "batch" entries). Other subdirs
        (code samples, lambda hooks) are skipped. Called alongside
        write_catalog_file so it rides the same config_library sync to the
        ConfigurationBucket. Each entry's ``s3Key`` matches where
        upload_samples() / CopySampleFiles land the binary in the stack's
        ConfigurationBucket (``samples/<file>``), and ``configId`` records the
        associated config_library/unified preset (or None), so the UI can offer
        to import + use the matching config when the sample is launched.
        """
        samples_dir = Path(self._SAMPLES_DIR)
        if not samples_dir.is_dir():
            self.log_verbose(
                f"{self._SAMPLES_DIR}/ not found — skipping samples manifest"
            )
            return None

        samples = []
        for entry in sorted(os.listdir(samples_dir)):
            path = samples_dir / entry
            if path.is_file() and entry.lower().endswith(self._SAMPLE_DOC_EXTS):
                name, desc, config_id = self._sample_label(entry)
                samples.append(
                    {
                        "id": os.path.splitext(entry)[0],
                        "name": name,
                        "description": desc,
                        "s3Key": f"samples/{entry}",
                        "kind": "document",
                        "fileCount": 1,
                        "configId": config_id,
                    }
                )
            elif path.is_dir() and entry in self._SAMPLE_DOC_SUBDIRS:
                docs = [
                    f
                    for f in sorted(os.listdir(path))
                    if f.lower().endswith(self._SAMPLE_DOC_EXTS)
                ]
                if not docs:
                    continue
                name, desc, config_id = self._sample_label(entry)
                samples.append(
                    {
                        "id": entry,
                        "name": name,
                        "description": desc,
                        "s3Key": f"samples/{entry}/",
                        # s3Key of each file in the batch (the folder prefix is
                        # not openable; individual files are). Lets the agent
                        # cite one representative document as a viewable example.
                        "files": [f"samples/{entry}/{f}" for f in docs],
                        "kind": "batch",
                        "fileCount": len(docs),
                        "configId": config_id,
                    }
                )

        manifest = {"schemaVersion": "1.0", "samples": samples}
        out_path = Path(self._SAMPLES_MANIFEST_FILE)
        out_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        self.log_success(f"Wrote {out_path} ({len(samples)} sample documents)")
        return manifest

    def _upload_samples_manifest_to_artifacts(self):
        """Upload config_library/samples-manifest.json to the artifacts bucket.

        Mirrors _upload_catalog_to_artifacts: upload_config_library already
        bulk-synced config_library/ before this freshly-written file existed, so
        upload it on its own under the same prefix; the deploy-time copy FileList
        (generate_config_file_list walks the local config_library/ dir) then
        includes it and ConfigurationCopyFunction copies it into the stack's
        ConfigurationBucket.
        """
        out_path = Path(self._SAMPLES_MANIFEST_FILE)
        if not out_path.is_file():
            return
        s3_key = f"{self.prefix_and_version}/config_library/samples-manifest.json"
        self.s3_client.upload_file(
            str(out_path),
            self.bucket,
            s3_key,
            ExtraArgs={"ContentType": "application/json"},
        )
        self.log_verbose(
            f"Uploaded samples-manifest.json to s3://{self.bucket}/{s3_key}"
        )

    def iter_sample_files(self):
        """Yield sample document files as paths relative to samples/.

        Applies the SAME selection rules as generate_samples_manifest: top-level
        files with a document extension, plus the document extensions inside the
        known batch subdirectories. Code/hook subdirs and non-document files
        (.xlsx, .docx, ...) are skipped, so we never ship
        lambda-hook-inference/, external-mcp-client/, etc.
        """
        samples_dir = Path(self._SAMPLES_DIR)
        if not samples_dir.is_dir():
            return
        for entry in sorted(os.listdir(samples_dir)):
            path = samples_dir / entry
            if path.is_file() and entry.lower().endswith(self._SAMPLE_DOC_EXTS):
                yield entry
            elif path.is_dir() and entry in self._SAMPLE_DOC_SUBDIRS:
                for f in sorted(os.listdir(path)):
                    if f.lower().endswith(self._SAMPLE_DOC_EXTS):
                        yield f"{entry}/{f}"

    def generate_sample_file_list(self):
        """List of sample document files (relative to samples/) for copying.

        Consumed by the deploy-time CopySampleFiles custom resource
        (<SAMPLE_FILES_LIST_TOKEN>) so ConfigurationCopyFunction copies each
        binary from the artifacts bucket into the stack's ConfigurationBucket
        under samples/.
        """
        return sorted(self.iter_sample_files())

    def upload_samples(self):
        """Upload curated sample document binaries to the artifacts bucket.

        Mirrors upload_config_library, but uploads only the curated document
        files (see iter_sample_files) to
        s3://{bucket}/{prefix_and_version}/samples/. The deploy-time
        CopySampleFiles custom resource then copies them into the stack's
        ConfigurationBucket under samples/, where the samples-manifest s3Key
        values point and the upload_resolver reads them.
        """
        self.log_phase("Uploading Sample Documents", "📄")
        files = self.generate_sample_file_list()
        if not files:
            self.log_verbose(f"No sample documents found under {self._SAMPLES_DIR}/")
            return
        self.log_task(f"Uploading {len(files)} sample documents to S3...")
        for rel_path in files:
            local_path = os.path.join(self._SAMPLES_DIR, rel_path)
            s3_key = f"{self.prefix_and_version}/samples/{rel_path}"
            self.s3_client.upload_file(local_path, self.bucket, s3_key)
        self.log_success(f"Uploaded {len(files)} sample documents")

    def _normalize_marketplace_regions(self, item):
        """Normalize a marketplace entry's per-region artifact map (schema 1.1).

        Returns ``{region: {"sellerBucket": ..., "templateKey": ...}}``.

        Schema 1.1 entries carry an explicit ``regions`` mapping. Legacy 1.0
        entries carry flat ``sellerBucket`` / ``sellerBucketRegion`` /
        ``templateKey`` fields describing a SINGLE region; those are folded into
        a one-entry map here so the host only ever has to read ``regions``.

        A legacy entry with no ``sellerBucketRegion`` cannot be placed on the map
        at all — we refuse to guess, because guessing is exactly the bug schema
        1.1 exists to fix (the old resolver used the entry's bucket in every
        region, handing customers a template whose baked CodeUri pointed at
        another region's objects). Such an entry still publishes with its legacy
        fields intact and the host reports "not available in <region>".
        """
        raw_regions = item.get("regions")
        normalized = {}
        if isinstance(raw_regions, dict):
            for region, spec in raw_regions.items():
                if not isinstance(spec, dict):
                    self.log_warning(
                        f"Marketplace extension {item['featureId']!r}: ignoring "
                        f"malformed regions[{region!r}] entry {spec!r}"
                    )
                    continue
                bucket = (spec.get("sellerBucket") or "").strip()
                key = (spec.get("templateKey") or "").strip().lstrip("/")
                if not (bucket and key):
                    self.log_warning(
                        f"Marketplace extension {item['featureId']!r}: "
                        f"regions[{region!r}] needs both sellerBucket and "
                        f"templateKey; skipping that region"
                    )
                    continue
                normalized[str(region).strip()] = {
                    "sellerBucket": bucket,
                    "templateKey": key,
                }
        elif raw_regions is not None:
            self.log_warning(
                f"Marketplace extension {item['featureId']!r}: 'regions' must be a "
                f"mapping of region -> {{sellerBucket, templateKey}}; got "
                f"{type(raw_regions).__name__}"
            )

        if normalized:
            return normalized

        # Legacy schema 1.0 fallback: fold the flat single-region fields in.
        legacy_bucket = (item.get("sellerBucket") or "").strip()
        legacy_region = (item.get("sellerBucketRegion") or "").strip()
        legacy_key = (item.get("templateKey") or "").strip().lstrip("/")
        if legacy_bucket and legacy_key and legacy_region:
            self.log_verbose(
                f"Marketplace extension {item['featureId']!r} uses the deprecated "
                f"flat sellerBucket/sellerBucketRegion schema; normalized to "
                f"regions[{legacy_region!r}]. Migrate to a 'regions' map."
            )
            return {
                legacy_region: {
                    "sellerBucket": legacy_bucket,
                    "templateKey": legacy_key,
                }
            }
        if legacy_bucket or legacy_key:
            self.log_warning(
                f"Marketplace extension {item['featureId']!r} has no usable region "
                f"mapping (needs a 'regions' map, or sellerBucket + "
                f"sellerBucketRegion + templateKey). The host will report it as "
                f"unavailable in every region."
            )
        return {}

    # Mirrors _LICENSE_MODES in check_feature_entitlement/index.py.
    _LICENSE_MODES = ("none", "simulated", "marketplace-live")

    def _marketplace_license_mode(self, item):
        """Validated `licenseMode` for one marketplace catalog entry.

        Absent → `marketplace-live` (see the call site for why the host's default
        is the strict one). An unrecognised value is a hard error rather than a
        silent downgrade: a typo here decides which authority confirms a paid
        subscription, and getting that wrong quietly is the failure this whole
        field exists to prevent.
        """
        raw = item.get("licenseMode")
        if raw is None or raw == "":
            return "marketplace-live"
        mode = str(raw).strip()
        if mode not in self._LICENSE_MODES:
            self.log_error(
                f"{self._MARKETPLACE_EXTENSIONS_FILE}: feature "
                f"{item.get('featureId')!r} has licenseMode={raw!r}, which is not "
                f"one of {', '.join(self._LICENSE_MODES)}."
            )
            sys.exit(1)
        return mode

    def _load_marketplace_features(self):
        """Load + normalize the curated marketplace extension list.

        Returns a list of CatalogFeature dicts (source='marketplace'). Missing
        file → empty list (OSS-only deployment). Malformed file is a hard error
        — a broken catalog would silently hide paid extensions.
        """
        path = Path(self._MARKETPLACE_EXTENSIONS_FILE)
        if not path.is_file():
            self.log_verbose(
                f"{self._MARKETPLACE_EXTENSIONS_FILE} not found — catalog will list "
                f"OSS features only."
            )
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self.log_error(f"Malformed {self._MARKETPLACE_EXTENSIONS_FILE}: {exc}")
            sys.exit(1)
        raw = data.get("features") or []
        entries = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("featureId"):
                self.log_warning(
                    f"Skipping malformed marketplace extension entry: {item!r}"
                )
                continue
            entries.append(
                {
                    "featureId": item["featureId"],
                    "displayName": item.get("displayName") or item["featureId"],
                    "description": item.get("description") or "",
                    "iconUrl": item.get("iconUrl") or "",
                    # Optional absolute docs URL; if omitted the UI falls back to
                    # marketplaceListingUrl for the "Learn more" link.
                    "docsUrl": item.get("docsUrl") or "",
                    # Not-yet-installed nav visibility (default True). Set
                    # false for entries that should only be discoverable via
                    # the UI's Browse catalog page.
                    "showInNav": bool(item.get("showInNav", True)),
                    "source": "marketplace",
                    # Seed/fallback only. The host reads <base>/latest.json at
                    # runtime for the "Update available" badge, so an extension
                    # release no longer requires re-publishing the host.
                    "latestVersion": item.get("latestVersion") or "",
                    # productCode drives the seller-side GetEntitlements API;
                    # productId drives the buyer-side Agreement API
                    # (SearchAgreements ResourceIdentifier filter), which is what
                    # the host can actually call from the customer's account.
                    "productCode": item.get("productCode") or "",
                    "productId": item.get("productId") or "",
                    "marketplaceListingUrl": item.get("marketplaceListingUrl") or "",
                    # Which authority the HOST must check this extension against:
                    # "none" | "simulated" | "marketplace-live". Per-extension, so
                    # one stack can confirm a listed product against real AWS
                    # Marketplace while checking in-development extensions against
                    # a simulator.
                    #
                    # Defaulted HERE, at publish time, rather than at runtime, and
                    # that placement is load-bearing: the resolver's fallback chain
                    # is row → catalog → legacy stack setting, so a runtime default
                    # would shadow the legacy step and change behaviour for a stack
                    # running an older catalog. Baking it means a current catalog
                    # always carries an explicit value and the legacy step is
                    # reached only by a catalog that genuinely predates the field.
                    #
                    # The default is `marketplace-live` — deliberately the OPPOSITE
                    # of the extension-side default (`none`). An unrecognised value
                    # must not OVER-CLAIM verification for something listed in the
                    # *marketplace* catalog, so the host degrades to the strictest
                    # authority; the extension degrades to serving so it can't lock
                    # a paying customer out. See docs/feature-platform.md.
                    "licenseMode": self._marketplace_license_mode(item),
                    # Schema 1.1: explicit per-region bucket + version-free
                    # template key. Always present (possibly empty) so the host
                    # reads exactly one shape.
                    "regions": self._normalize_marketplace_regions(item),
                    # Deprecated schema 1.0 fields, emitted verbatim so a host
                    # older than this catalog keeps working. Absent from
                    # `regions`-only entries ON PURPOSE: an old host would then
                    # fail loudly ("catalog entry is incomplete") instead of
                    # silently reusing one region's bucket everywhere, which is
                    # the wrong-region deploy failure schema 1.1 exists to fix.
                    "sellerBucket": item.get("sellerBucket") or "",
                    "sellerBucketRegion": item.get("sellerBucketRegion") or "",
                    "templateKey": item.get("templateKey") or "",
                }
            )
        return entries

    def write_catalog_file(self, oss_catalog_entries):
        """Merge OSS + marketplace entries and write config_library/catalog.json.

        Called after build_and_upload_sample_features() and BEFORE
        upload_config_library() / generate_config_file_list() so the file is
        synced to the artifacts bucket and included in the deploy-time copy
        FileList. The host never lists buckets — it reads this one file from its
        own ConfigurationBucket.
        """
        marketplace_entries = self._load_marketplace_features()
        # De-dupe by featureId; OSS bundled features win over a marketplace
        # entry of the same id (defensive — they shouldn't collide).
        by_id = {}
        for entry in marketplace_entries + list(oss_catalog_entries):
            by_id[entry["featureId"]] = entry
        catalog = {
            # 1.1 adds `regions` (per-region sellerBucket + version-free
            # templateKey) and `productId` to marketplace entries. Purely
            # additive — every 1.0 field is still emitted — so the version is
            # informational and no consumer gates behavior on it.
            "schemaVersion": "1.1",
            "features": sorted(by_id.values(), key=lambda f: f["displayName"].lower()),
        }
        out_path = Path(self._CATALOG_OUTPUT_FILE)
        out_path.write_text(
            json.dumps(catalog, indent=2, sort_keys=True), encoding="utf-8"
        )
        self.log_success(
            f"Wrote {out_path} ({len(catalog['features'])} features: "
            f"{len(oss_catalog_entries)} OSS + {len(marketplace_entries)} marketplace)"
        )
        return catalog

    def _upload_catalog_to_artifacts(self):
        """Upload config_library/catalog.json to the artifacts bucket.

        upload_config_library() already ran (it bulk-syncs config_library/
        before sample features are built), so this uploads the freshly-written
        catalog.json on its own. It lands under the same
        `<prefix>/config_library/` path, so the deploy-time copy FileList
        (generate_config_file_list, which walks the local config_library/ dir)
        includes it and the ConfigurationCopyFunction copies it into the stack's
        ConfigurationBucket.
        """
        out_path = Path(self._CATALOG_OUTPUT_FILE)
        if not out_path.is_file():
            return
        s3_key = f"{self.prefix_and_version}/config_library/catalog.json"
        self.s3_client.upload_file(
            str(out_path),
            self.bucket,
            s3_key,
            ExtraArgs={"ContentType": "application/json"},
        )
        self.log_verbose(f"Uploaded catalog.json to s3://{self.bucket}/{s3_key}")

    def _build_and_upload_single_feature(
        self, feature_dir, load_manifest, FeaturePublisher
    ):
        """Build and upload one bundled feature. Returns its uploaded file list."""
        _PUBLISH_FORMAT_VERSION = "v3-multi-feature"

        deps = self._get_feature_deps(feature_dir)
        current_checksum = hashlib.sha256(
            (
                _PUBLISH_FORMAT_VERSION
                + "".join(
                    self.get_file_checksum(d)
                    if os.path.isfile(d)
                    else self.get_source_files_checksum(d)
                    for d in deps
                    if os.path.exists(d)
                )
            ).encode()
        ).hexdigest()

        bundle_path = feature_dir / "feature-ui" / "dist" / "ui-bundle.js"
        checksum_file = feature_dir / ".checksum"

        cached = False
        if checksum_file.is_file() and bundle_path.is_file():
            try:
                cached = (
                    checksum_file.read_text(encoding="utf-8").strip()
                    == current_checksum
                )
            except OSError:
                cached = False

        # A cache hit is only trustworthy if the on-disk bundle actually carries
        # the manifest version. Vite bakes feature.yaml -> version into the
        # bundle at build time; the host's FeatureLoader refuses to run a bundle
        # whose self-reported version differs from the registered version
        # ("bundle version X does not match registered Y"), silently serving old
        # code. A checksum hit against a stale `dist/` (e.g. the version was
        # bumped but the source checksum collided, or dist/ predates the bump)
        # would otherwise ship wrong-version code to the new version's S3 key.
        # So: treat a version-mismatched cached bundle as a cache MISS and
        # rebuild, rather than trusting the checksum alone.
        def _bundle_has_version(version: str) -> bool:
            if not bundle_path.is_file():
                return False
            try:
                return f'"{version}"' in bundle_path.read_text(encoding="utf-8")
            except OSError:
                return False

        if cached:
            manifest = load_manifest(feature_dir)
            # A cache hit skips publisher.build(), so any artifact the upload
            # step needs must already be on disk. Besides the versioned UI
            # bundle, a feature with an agentSource must still have its packaged
            # zip (package_agent_source.sh output) — it is git-ignored and gets
            # cleaned between runs, so treat a missing zip as a cache MISS.
            agent_source = getattr(manifest, "agentSource", None)
            agent_zip_missing = bool(
                agent_source
                and getattr(agent_source, "artifactPath", None)
                and not (feature_dir / agent_source.artifactPath).is_file()
            )
            if _bundle_has_version(manifest.version) and not agent_zip_missing:
                self.log_cached(
                    f"Feature {feature_dir.name} source unchanged — "
                    f"using cached UI bundle"
                )
            else:
                reason = (
                    "agent-source.zip missing"
                    if agent_zip_missing
                    else f"cached bundle does not carry version '{manifest.version}'"
                )
                self.log_warning(
                    f"Feature {feature_dir.name}: {reason} — forcing a rebuild."
                )
                cached = False

        if not cached:
            publisher = FeaturePublisher(feature_dir, console=self.console)
            try:
                manifest = publisher.validate()
                publisher.build(manifest)
            except Exception as exc:  # noqa: BLE001 — surface any build failure
                self.log_error(f"Feature {feature_dir.name} build failed: {exc}")
                sys.exit(1)
            try:
                checksum_file.write_text(current_checksum, encoding="utf-8")
            except OSError as exc:
                self.log_warning(f"Could not write {checksum_file}: {exc}")

        # Final guard: after build (cached or fresh) the bundle MUST carry the
        # manifest version, or we'd upload wrong-version code to the
        # manifest-version S3 key. Fail loudly — never publish a mismatch.
        if not _bundle_has_version(manifest.version):
            self.log_error(
                f"Feature {feature_dir.name}: built UI bundle does not contain "
                f"the manifest version '{manifest.version}' even after a rebuild. "
                f"Check feature-ui/vite.config.ts version injection and that "
                f"`npm run build` regenerates feature-ui/dist/ui-bundle.js."
            )
            sys.exit(1)

        # sam build + sam package on the feature's SAM template so local
        # CodeUri: paths are rewritten to s3://...
        self.log_task(
            f"Packaging {feature_dir.name} SAM template (sam build + sam package)"
        )
        self.build_and_package_template(str(feature_dir), force_rebuild=True)
        packaged_template_path = feature_dir / ".aws-sam" / "packaged.yaml"
        if not packaged_template_path.is_file():
            self.log_error(
                f"Expected packaged template at {packaged_template_path} "
                f"but it was not produced by sam package"
            )
            sys.exit(1)

        file_list = self._upload_sample_feature_artifacts(
            feature_dir,
            manifest,
            bundle_path,
            packaged_template_path=packaged_template_path,
        )

        # Catalog entry for this OSS feature — discovery metadata only; the
        # actual template URL is computed at request time by getFeatureLaunchUrl
        # from the stack-owned FeatureBucket (no artifacts-bucket dependency).
        catalog_entry = {
            "featureId": manifest.featureId,
            "displayName": manifest.displayName,
            "description": manifest.description or "",
            "iconUrl": manifest.iconUrl or "",
            "docsUrl": manifest.docsUrl or "",
            # From feature.yaml showInNav (default True): whether the feature
            # gets its own nav entry before it's installed. The bundled
            # samples set false so they're only in the Browse catalog page.
            "showInNav": manifest.showInNav,
            "source": "oss",
            # Open-source extensions have no Marketplace contract, so there is no
            # authority to check. extensions-oss.yaml needs no field for this —
            # absent means `none` there — but the CATALOG states it explicitly so
            # every entry the host reads has one shape.
            "licenseMode": "none",
            "latestVersion": manifest.version,
            # OSS extension artifacts live in the (same) artifacts bucket the
            # main template is published to, under a VERSION-FREE extension base
            # `<prefix>/extensions/<id>`. The host's getFeatureLaunchUrl builds
            # the (version-free) Launch Stack template URL from artifactPrefix +
            # "/template.yaml"; the feature template self-locates its versioned
            # artifacts under `<artifactPrefix>/<version>/...` from its baked
            # FEATURE_VERSION. No separate, stack-owned feature bucket needed.
            "artifactBucket": self.bucket,
            "artifactPrefix": f"{self.prefix}/extensions/{manifest.featureId}",
        }
        return file_list, catalog_entry

    def _upload_sample_feature_artifacts(
        self, feature_dir, manifest, bundle_path, packaged_template_path=None
    ):
        """Upload a built sample feature's artifacts to the artifact bucket.

        Uses a VERSION-FREE extension base, mirroring how the main template is
        published (see _upload_version_pointer, which keys off self.prefix not
        self.prefix_and_version):

            <prefix>/extensions/<id>/template.yaml      # version-free; newest publish wins
            <prefix>/extensions/<id>/latest.json        # version-free pointer
            <prefix>/extensions/<id>/<version>/ui-bundle.js
            <prefix>/extensions/<id>/<version>/<configPreset.path>
            <prefix>/extensions/<id>/<version>/manifest.json

        The template is version-free so the host's getFeatureLaunchUrl points at
        a stable URL (and a stack Update sees the newest template), while the
        version-specific artifacts live under a <version>/ subfolder that the
        feature template self-locates from its baked FEATURE_VERSION — so no
        version-bearing value is stored as a stale-able CloudFormation
        parameter. Returns the list of uploaded relative paths (relative to the
        extension base).
        """
        feature_id = manifest.featureId
        version = manifest.version
        extension_root = f"{self.prefix}/extensions/{feature_id}"
        version_root = f"{version}"
        uploaded = []

        def _put(body, rel_key, content_type):
            self.s3_client.put_object(
                Bucket=self.bucket,
                Key=f"{extension_root}/{rel_key}",
                Body=body,
                ContentType=content_type,
            )
            uploaded.append(rel_key)

        def _upload(local_path, rel_key, content_type):
            self.s3_client.upload_file(
                str(local_path),
                self.bucket,
                f"{extension_root}/{rel_key}",
                ExtraArgs={"ContentType": content_type},
            )
            uploaded.append(rel_key)

        # template.yaml — VERSION-FREE path; prefer the sam-packaged version
        # (CodeUri rewritten to s3://...). Bake two literals into the template
        # at publish time (NOT CloudFormation parameters — the CFN console's
        # "Update stack" wizard drops params on a template change, which left
        # FeatureArtifactPrefix empty and produced a `//<version>/` bad S3 key):
        #   <FEATURE_VERSION_TOKEN>          -> manifest.version
        #   <FEATURE_ARTIFACT_PREFIX_TOKEN>  -> <prefix>/extensions/<id>
        # The ui-deployer reads <prefix>/extensions/<id>/<version>/... — both
        # halves baked, so a stack Update can never carry a stale or empty
        # value.
        template_source = (
            packaged_template_path
            if packaged_template_path is not None
            else feature_dir / manifest.template.path
        )
        template_text = Path(template_source).read_text(encoding="utf-8")
        baked_text = template_text.replace("<FEATURE_VERSION_TOKEN>", version).replace(
            "<FEATURE_ARTIFACT_PREFIX_TOKEN>", extension_root
        )
        _put(
            baked_text.encode("utf-8"),
            "template.yaml",
            "application/x-yaml",
        )

        _upload(
            bundle_path,
            f"{version_root}/ui-bundle.js",
            "application/javascript",
        )

        # Config preset — if the manifest declares one. The feature stack's
        # ui-deployer downloads it from
        # `<FeatureArtifactPrefix>/<version>/<configPreset.path>` at install to
        # call applyFeatureConfigPreset, so it MUST be uploaded at that same
        # relative path under the version subfolder.
        config_preset = getattr(manifest, "configPreset", None)
        if config_preset and getattr(config_preset, "path", None):
            preset_local = feature_dir / config_preset.path
            if preset_local.is_file():
                preset_ct = (
                    "application/x-yaml"
                    if preset_local.suffix.lower() in (".yaml", ".yml")
                    else "application/json"
                )
                _upload(
                    preset_local,
                    f"{version_root}/{config_preset.path}",
                    preset_ct,
                )
            else:
                self.log_error(
                    f"Feature {feature_id} declares configPreset.path "
                    f"'{config_preset.path}' but no file exists at {preset_local}"
                )
                sys.exit(1)

        # Agent source zip — if the manifest declares an agentSource. The
        # feature stack's CodeBuild project reads it from
        # `<FEATURE_ARTIFACT_PREFIX>/<version>/<artifactPath>` (Source.Location)
        # to build the AgentCore Runtime image at install, so it MUST be uploaded
        # at that same relative path under the version subfolder. The publisher
        # (publisher.build) already produced the zip at feature_dir/<artifactPath>.
        agent_source = getattr(manifest, "agentSource", None)
        if agent_source and getattr(agent_source, "artifactPath", None):
            agent_zip_local = feature_dir / agent_source.artifactPath
            if agent_zip_local.is_file():
                _upload(
                    agent_zip_local,
                    f"{version_root}/{agent_source.artifactPath}",
                    "application/zip",
                )
            else:
                self.log_error(
                    f"Feature {feature_id} declares agentSource.artifactPath "
                    f"'{agent_source.artifactPath}' but no file exists at "
                    f"{agent_zip_local} — the package step must produce it before "
                    f"upload. Check agentSource.package / packageCommand."
                )
                sys.exit(1)

        manifest_data = {
            "featureId": feature_id,
            "displayName": manifest.displayName,
            "version": version,
            "description": manifest.description,
            "iconUrl": manifest.iconUrl,
            "capabilities": list(manifest.capabilities),
            "defaultParameters": dict(manifest.defaultParameters),
            "marketplace": {
                "productCode": manifest.marketplace.productCode,
                "listingUrl": manifest.marketplace.listingUrl,
            },
        }
        _put(
            json.dumps(manifest_data, indent=2, sort_keys=True).encode("utf-8"),
            f"{version_root}/manifest.json",
            "application/json",
        )

        # Version-free pointer (mirrors idp-main-latest.json). Not read by the
        # host resolver at runtime (it reads catalog.json) — kept for parity and
        # ad-hoc inspection.
        latest_data = {
            "featureId": feature_id,
            "version": version,
            "displayName": manifest.displayName,
            "publishedAt": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        _put(
            json.dumps(latest_data, indent=2, sort_keys=True).encode("utf-8"),
            "latest.json",
            "application/json",
        )

        self.log_success(
            f"Uploaded {len(uploaded)} extension artifacts to "
            f"s3://{self.bucket}/{extension_root}/ ({feature_id} v{version})"
        )
        return sorted(uploaded)

    def build_main_template(
        self,
        webui_zipfile,
        unified_source_zipfile,
        components_needing_rebuild,
        sample_features_hash="",
        sample_features_list=None,
    ):
        """Build and package main template with smart detection.

        ``sample_features_hash`` / ``sample_features_list`` carry the result of
        :meth:`build_and_upload_sample_features` so the main template's
        PublishSampleFeature custom resource knows what to copy into the
        auto-created feature bucket and re-runs when the bundled feature
        source changes. Both default to empty so non-feature-platform builds
        (and callers that don't bundle features) work unchanged.
        """
        try:
            self.console.print("[bold cyan]BUILDING main[/bold cyan]")
            # Main template needs rebuilding, if any component needs rebuilding
            if components_needing_rebuild:
                self.console.print("[yellow]Main template needs rebuilding[/yellow]")
                # Validate Python syntax in src directory before building
                if not self._validate_python_syntax("src"):
                    raise Exception("Python syntax validation failed")

                # Build main template with progress indicator
                # Lambda functions now use Lambda Layers instead of bundled dependencies
                cmd = [
                    "sam",
                    "build",
                    "--parallel",  # Safe with Lambda Layers
                    "--template-file",
                    "template.yaml",
                ]
                if self.use_container_flag and self.use_container_flag.strip():
                    cmd.append(self.use_container_flag)

                # Use spinner progress indicator for SAM build
                sam_build_start = time.time()
                success = False
                try:
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        TimeElapsedColumn(),
                        console=self.console,
                        transient=False,
                    ) as progress:
                        task = progress.add_task(
                            "[cyan]Building main template (SAM build --parallel)...",
                            total=None,
                        )
                        success, result = self.run_subprocess_with_logging(
                            cmd, "Main template SAM build"
                        )
                        sam_build_elapsed = time.time() - sam_build_start
                        if success:
                            progress.update(
                                task,
                                description=f"[green]✓ SAM build completed in {sam_build_elapsed:.1f}s",
                            )
                        else:
                            progress.update(
                                task,
                                description=f"[red]✗ SAM build failed after {sam_build_elapsed:.1f}s",
                            )
                except Exception:
                    # Re-raise the exception to be caught by outer try/finally
                    raise

                if not success:
                    # Delete main template checksum on build failure
                    raise Exception("SAM build failed")

                self.console.print("[bold cyan]PACKAGING main[/bold cyan]")

                # Read the template
                with open(".aws-sam/build/template.yaml", "r") as f:
                    template_content = f.read()

                # Get configuration file list
                config_files_list = self.generate_config_file_list()
                config_files_json = json.dumps(config_files_list)

                # Sample document file list + content hash for the deploy-time
                # CopySampleFiles custom resource. Hash over the curated files'
                # contents so a changed sample (even without a rename)
                # re-triggers the copy into the ConfigurationBucket.
                sample_files_list = self.generate_sample_file_list()
                sample_files_json = json.dumps(sample_files_list)
                sample_hash_material = "".join(
                    rel + self.get_file_checksum(os.path.join(self._SAMPLES_DIR, rel))
                    for rel in sample_files_list
                )
                samples_hash = hashlib.sha256(
                    sample_hash_material.encode()
                ).hexdigest()[:16]

                # Extract content-based hash from unified source zipfile name for ImageVersion
                # Format: unified-source-{hash}.zip -> extract {hash}
                unified_image_version = unified_source_zipfile.replace(
                    "unified-source-", ""
                ).replace(".zip", "")

                # Get various hashes
                workforce_url_file = "src/lambda/get-workforce-url/index.py"
                a2i_resources_file = "src/lambda/create_a2i_resources/index.py"
                cognito_client_file = "src/lambda/cognito_updater_hitl/index.py"

                workforce_url_hash = (
                    self.get_file_checksum(workforce_url_file)[:16]
                    if os.path.exists(workforce_url_file)
                    else ""
                )
                a2i_resources_hash = (
                    self.get_file_checksum(a2i_resources_file)[:16]
                    if os.path.exists(a2i_resources_file)
                    else ""
                )
                cognito_client_hash = (
                    self.get_file_checksum(cognito_client_file)[:16]
                    if os.path.exists(cognito_client_file)
                    else ""
                )

                # Replace tokens in template

                build_date_time = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                replacements = {
                    "<VERSION>": self.version,
                    "<BUILD_DATE_TIME>": build_date_time,
                    "<PUBLIC_SAMPLE_UDOP_MODEL>": self.public_sample_udop_model,
                    "<ARTIFACT_BUCKET_TOKEN>": self.bucket,
                    "<ARTIFACT_PREFIX_TOKEN>": self.prefix_and_version,
                    # Public template version-check defaults — substituted into
                    # template.yaml so the deployed stack's CloudFormation
                    # parameters default to the bucket/prefix the operator is
                    # publishing TO. This makes the Web UI "Update available"
                    # indicator work out of the box for whoever publishes (e.g.
                    # the public release publishes to aws-ml-blog-<region> with
                    # prefix `artifacts/genai-idp` and gets the indicator for
                    # free; private builders get a default that points at their
                    # own bucket/prefix).
                    #
                    # `prefix` is the version-stripped prefix (e.g.
                    # `artifacts/genai-idp`), so the resolver lists sibling
                    # versioned templates published by future `publish` runs.
                    "<PUBLIC_ARTIFACTS_BUCKET_TOKEN>": self.bucket,
                    "<PUBLIC_ARTIFACTS_PREFIX_TOKEN>": self.prefix,
                    "<WEBUI_ZIPFILE_TOKEN>": webui_zipfile,
                    "<UNIFIED_SOURCE_ZIPFILE_TOKEN>": unified_source_zipfile,
                    # Unified image version extracted from source zipfile hash
                    "<UNIFIED_IMAGE_VERSION>": unified_image_version,
                    # Lambda Layer zip filenames
                    "<IDP_COMMON_BASE_LAYER_ZIP>": self._layer_arns.get("base", {}).get(
                        "zip_name", "idp-common-base.zip"
                    ),
                    "<IDP_COMMON_REPORTING_LAYER_ZIP>": self._layer_arns.get(
                        "reporting", {}
                    ).get("zip_name", "idp-common-reporting.zip"),
                    "<IDP_COMMON_AGENTS_LAYER_ZIP>": self._layer_arns.get(
                        "agents", {}
                    ).get("zip_name", "idp-common-agents.zip"),
                    "<IDP_COMMON_MULTI_DOC_DISCOVERY_LAYER_ZIP>": self._layer_arns.get(
                        "multi_document_discovery", {}
                    ).get("zip_name", "idp-common-multi_document_discovery.zip"),
                    "<HASH_TOKEN>": self.get_directory_checksum("./lib")[:16],
                    "<LAMBDA_HASH_TOKEN>": self.get_directory_checksum(
                        "./src/lambda/agentcore_gateway_manager"
                    )[:16],
                    # Include config_library + config processing code (Lambda, models, system defaults)
                    # This ensures UpdateConfiguration custom resource re-runs when processing logic changes
                    "<CONFIG_LIBRARY_HASH_TOKEN>": hashlib.sha256(
                        (
                            self.get_directory_checksum("config_library")
                            + self.get_directory_checksum(
                                "src/lambda/update_configuration"
                            )
                            + self.get_directory_checksum(
                                "lib/idp_common_pkg/idp_common/config"
                            )
                        ).encode()
                    ).hexdigest()[:16],
                    "<CONFIG_FILES_LIST_TOKEN>": config_files_json,
                    # Sample document binaries copied into the ConfigurationBucket
                    # under samples/ at deploy time (CopySampleFiles).
                    "<SAMPLES_HASH_TOKEN>": samples_hash,
                    "<SAMPLE_FILES_LIST_TOKEN>": sample_files_json,
                    "<WORKFORCE_URL_HASH_TOKEN>": workforce_url_hash,
                    "<A2I_RESOURCES_HASH_TOKEN>": a2i_resources_hash,
                    "<COGNITO_CLIENT_HASH_TOKEN>": cognito_client_hash,
                    "<FCC_DATASET_DEPLOYER_HASH_TOKEN>": self.get_directory_checksum(
                        "src/lambda/fcc_dataset_deployer"
                    )[:16],
                    "<OCR_BENCHMARK_DEPLOYER_HASH_TOKEN>": self.get_directory_checksum(
                        "src/lambda/ocr_benchmark_deployer"
                    )[:16],
                    "<docsplit_testset_deployer_HASH_TOKEN>": self.get_directory_checksum(
                        "src/lambda/docsplit_testset_deployer"
                    )[:16],
                    "<w2_dataset_deployer_HASH_TOKEN>": self.get_directory_checksum(
                        "src/lambda/w2_dataset_deployer"
                    )[:16],
                    # BuildHash is the ONLY meaningful property of the
                    # DockerBuildRun custom resource, so it is the sole thing
                    # that re-triggers the container build on a stack update: if
                    # it doesn't change, CloudFormation sees no delta, never
                    # re-invokes the resource, and the ECR :latest image keeps
                    # whatever it had. It must therefore cover EVERY input to the
                    # image — the handler code AND the Dockerfile/requirements.txt
                    # the build installs from. Hashing only the handler directory
                    # meant a Dependabot bump to requirements.txt left BuildHash
                    # byte-identical and the vulnerable dependency stayed
                    # deployed (a fresh create always builds, so this is only
                    # observable on an in-place update).
                    "<MULTI_DOC_DISCOVERY_BUILD_HASH_TOKEN>": hashlib.sha256(
                        (
                            self.get_directory_checksum(
                                "src/lambda/multi_doc_discovery"
                            )
                            + "".join(
                                self.get_file_checksum(build_input)
                                for build_input in MULTI_DOC_DISCOVERY_BUILD_INPUTS
                            )
                        ).encode()
                    ).hexdigest()[:16],
                    "<SAMPLE_FEATURES_HASH_TOKEN>": sample_features_hash,
                    "<SAMPLE_FEATURES_LIST_TOKEN>": json.dumps(
                        sample_features_list or []
                    ),
                }

                # Debug: show layer ARNs being used
                self.console.print(
                    f"[dim]Layer ARNs for token replacement: {list(self._layer_arns.keys())}[/dim]"
                )
                for layer_name, layer_info in self._layer_arns.items():
                    self.console.print(
                        f"[dim]  {layer_name}: {layer_info.get('zip_name', 'NOT SET')}[/dim]"
                    )

                self.log_verbose("Inline edit main template to replace:")
                for token, value in replacements.items():
                    self.log_verbose(f"   {token} with: {value}")
                    template_content = template_content.replace(token, value)

                # Write the modified template to the build directory
                build_packaged_template_path = ".aws-sam/build/idp-main.yaml"
                with open(build_packaged_template_path, "w") as f:
                    f.write(template_content)

                # Package the template from the build directory with progress indicator
                original_cwd = os.getcwd()
                os.chdir(".aws-sam/build")
                cmd = [
                    "sam",
                    "package",
                    "--template-file",
                    "idp-main.yaml",
                    "--output-template-file",
                    "../../.aws-sam/idp-main.yaml",
                    "--s3-bucket",
                    self.bucket,
                    "--s3-prefix",
                    self.prefix_and_version,
                ]
                self.log_verbose(
                    f"Running main template SAM package command: {' '.join(cmd)}"
                )

                # Use spinner progress indicator for SAM package
                sam_package_start = time.time()
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    TimeElapsedColumn(),
                    console=self.console,
                    transient=False,
                ) as progress:
                    task = progress.add_task(
                        "[cyan]Packaging main template (SAM package)...", total=None
                    )
                    success, result = self.run_subprocess_with_logging(
                        cmd, "Main template SAM package"
                    )
                    sam_package_elapsed = time.time() - sam_package_start
                    if success:
                        progress.update(
                            task,
                            description=f"[green]✓ SAM package completed in {sam_package_elapsed:.1f}s",
                        )
                    else:
                        progress.update(
                            task,
                            description=f"[red]✗ SAM package failed after {sam_package_elapsed:.1f}s",
                        )

                os.chdir(original_cwd)
                if not success:
                    raise Exception("SAM package failed")

                # Print main template build summary
                total_main_build_time = sam_build_elapsed + sam_package_elapsed
                self.console.print(
                    f"[dim]Main template: build={sam_build_elapsed:.1f}s, package={sam_package_elapsed:.1f}s, total={total_main_build_time:.1f}s[/dim]"
                )
            else:
                self.console.print("[green]✅ Main template is up to date[/green]")

            # Upload templates
            packaged_template_path = ".aws-sam/idp-main.yaml"
            templates = [
                (f"{self.prefix}/{self.main_template}", "Main template"),
                (
                    f"{self.prefix}/{self.main_template.replace('.yaml', f'_{self.version}.yaml')}",
                    "Versioned main template",
                ),
            ]

            for s3_key, description in templates:
                if components_needing_rebuild:
                    if not os.path.exists(packaged_template_path):
                        self.console.print(
                            f"[red]Error: Packaged template not found at {packaged_template_path}[/red]"
                        )
                        raise Exception(packaged_template_path + " missing")
                    self._upload_template_to_s3(
                        packaged_template_path, s3_key, description
                    )
                else:
                    self._check_and_upload_template(
                        packaged_template_path, s3_key, description
                    )

            # Write/overwrite the latest-version pointer at the (version-
            # stripped) prefix. The Web UI's getLatestPublishedVersion resolver
            # GetObjects this ONE known key (no ListObjectsV2 — works on the
            # public release bucket) to drive the Build Info "update available"
            # indicator. templateUrl points at the versioned template just
            # uploaded above.
            self._upload_version_pointer()

            # Validate the template
            if self.skip_validation:
                self.console.print(
                    "[yellow]⚠️  Skipping CloudFormation template validation[/yellow]"
                )
            else:
                template_url = f"https://s3.{self.region}.amazonaws.com/{self.bucket}/{templates[0][0]}"
                self.console.print(f"[cyan]Validating template: {template_url}[/cyan]")
                self.cf_client.validate_template(TemplateURL=template_url)
                self.console.print("[green]✅ Template validation passed[/green]")

        except ClientError as e:
            # Delete checksum on template validation failure
            self._delete_checksum_file(".checksum")
            self.console.print(
                "[red]❌ CloudFormation template validation failed[/red]"
            )
            self.console.print(str(e), style="red", markup=False)
            sys.exit(1)
        except Exception as e:
            # Delete checksum on any failure to force rebuild next time
            self._delete_checksum_file(".checksum")
            self.console.print("[red]❌ Main template build failed:[/red]")
            self.console.print(str(e), style="red", markup=False)
            sys.exit(1)

    def get_source_files_checksum(self, directory):
        """Get checksum of only source code files in a directory"""
        if not os.path.exists(directory):
            return ""

        # Cache directory checksums to avoid recalculation
        cache_key = f"source_checksum_{directory}"
        if hasattr(self, "_checksum_cache") and cache_key in self._checksum_cache:
            return self._checksum_cache[cache_key]

        if not hasattr(self, "_checksum_cache"):
            self._checksum_cache = {}

        # Use os.scandir for better performance than os.walk
        checksums = []
        file_count = 0

        # Define patterns once
        source_extensions = {
            ".py",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".yaml",
            ".yml",
            ".json",
            ".txt",
            ".toml",
            ".cfg",
            ".ini",
            ".graphql",
        }
        exclude_dirs = {
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "build",
            "dist",
            ".aws-sam",
            "node_modules",
            ".git",
            ".vscode",
            ".idea",
            "test-reports",
            ".coverage",
            "htmlcov",
            "coverage_html_report",
            "tests",
            "test",
        }

        def process_directory(dir_path):
            nonlocal file_count
            files_to_process = []
            try:
                with os.scandir(dir_path) as entries:
                    for entry in entries:
                        if entry.is_dir():
                            # Skip excluded directories by name and by suffix (e.g., *.egg-info)
                            if (
                                entry.name not in exclude_dirs
                                and not entry.name.startswith(".")
                                and not entry.name.endswith(".egg-info")
                            ):
                                process_directory(entry.path)
                        elif entry.is_file():
                            name = entry.name
                            if (
                                not name.startswith(".")
                                and not name.endswith(
                                    (".pyc", ".pyo", ".pyd", ".so", ".log", ".checksum")
                                )
                                and not name.startswith("test_")
                                and not name.endswith("_test.py")
                            ):
                                _, ext = os.path.splitext(name)
                                if (
                                    ext.lower() in source_extensions
                                    or name
                                    in {
                                        "Dockerfile",
                                        "Makefile",
                                        "requirements.txt",
                                        "setup.py",
                                        "setup.cfg",
                                    }
                                    or "template" in name.lower()
                                ):
                                    files_to_process.append(entry.path)

                # Sort files for deterministic order
                for file_path in sorted(files_to_process):
                    relative_path = os.path.relpath(file_path, directory)
                    file_checksum = self.get_file_checksum(file_path)
                    combined = f"{relative_path}:{file_checksum}"
                    checksums.append(hashlib.sha256(combined.encode()).hexdigest())
                    file_count += 1

            except (OSError, PermissionError):
                pass  # Skip inaccessible directories

        process_directory(directory)

        if self.verbose:
            self.console.print(
                f"[dim]Checksummed {file_count} source files in {directory}[/dim]"
            )

        # Combine all checksums
        combined = "".join(sorted(checksums))  # Sort for consistency
        result = hashlib.sha256(combined.encode()).hexdigest()

        # Cache the result
        self._checksum_cache[cache_key] = result
        return result

    def get_component_checksum(self, *paths):
        """Get combined checksum for component paths (source files only)"""
        # Use instance-level cache to avoid recalculating same paths
        if not hasattr(self, "_component_checksum_cache"):
            self._component_checksum_cache = {}

        # Include bucket and prefix in cache key to force rebuild when they change
        cache_key = (
            tuple(sorted(paths)),
            self.bucket,
            self.prefix_and_version,
            self.region,
        )
        if cache_key in self._component_checksum_cache:
            return self._component_checksum_cache[cache_key]

        checksums = []
        for path in paths:
            if os.path.isfile(path):
                # For individual files, use file checksum
                checksums.append(self.get_file_checksum(path))
            elif os.path.isdir(path):
                # For directories, use source files checksum
                checksums.append(self.get_source_files_checksum(path))

        # Include deployment context in checksum calculation
        combined = (
            "".join(checksums)
            + (self.bucket or "")
            + (self.prefix_and_version or "")
            + (self.region or "")
        )
        result = hashlib.sha256(combined.encode()).hexdigest()

        # Cache the result
        self._component_checksum_cache[cache_key] = result
        return result

    def get_component_dependencies(self):
        """Map each component to its dependencies for smart rebuild detection"""
        main_deps = ["./src", "template.yaml", "./config_library", LIB_DEPENDENCY]

        dependencies = {
            # Main template components
            "main": main_deps,
            # Nested components (includes all nested stacks - core and optional)
            "nested/api-resolvers": [
                LIB_DEPENDENCY,
                "nested/api-resolvers/src",
                "nested/api-resolvers/template.yaml",
            ],
            "nested/bedrockkb": [
                "nested/bedrockkb/src",
                "nested/bedrockkb/template.yaml",
            ],
            "nested/multi-doc-discovery": [
                LIB_DEPENDENCY,
                "nested/multi-doc-discovery/docker_build_lambda",
                "nested/multi-doc-discovery/template.yaml",
                # The container image's build inputs. Without these two, a
                # Dependabot bump to requirements.txt (or a Dockerfile change)
                # would not invalidate the checksum, so the smart-rebuild logic
                # would skip the component and keep shipping the old image.
                *MULTI_DOC_DISCOVERY_BUILD_INPUTS,
                "src/lambda/multi_doc_discovery",
            ],
            # Unified pattern (combines BDA + Pipeline)
            "patterns/unified": [
                LIB_DEPENDENCY,
                "patterns/unified/src",
                "patterns/unified/template.yaml",
                "patterns/unified/statemachine",
                "patterns/unified/buildspec.yml",
                "Dockerfile.optimized",
            ],
            # Feature Platform plumbing nested stack — InstalledFeatures DDB
            # table + AppSync resolvers/Lambdas. Deployed only when
            # EnableFeaturePlatform=true; built unconditionally so the nested
            # template URL the main stack references always resolves.
            "feature-platform/main-stack-extensions": [
                "feature-platform/main-stack-extensions/lambdas",
                "feature-platform/main-stack-extensions/template.yaml",
                "feature-platform/main-stack-extensions/appsync",
            ],
            "lib": [
                "./lib/idp_common_pkg"
            ],  # Include entire package, not just idp_common subdir
        }
        return dependencies

    def get_components_needing_rebuild(self):
        """Determine which components need rebuilding based on dependency changes"""
        dependencies = self.get_component_dependencies()
        components_to_rebuild = []

        # Cache checksums to avoid recalculating for shared dependencies (like ./lib)

        for component, deps in dependencies.items():
            # Use standard checksum file format: directory/.checksum
            if component == "main":
                checksum_file = ".checksum"
            elif component == "lib":
                checksum_file = "lib/.checksum"
            else:
                checksum_file = f"{component}/.checksum"

            # Calculate individual checksums for each dependency
            current_dep_checksums = {}
            for dep in deps:
                if os.path.isfile(dep):
                    current_dep_checksums[dep] = self.get_file_checksum(dep)
                elif os.path.isdir(dep):
                    current_dep_checksums[dep] = self.get_source_files_checksum(dep)
                else:
                    current_dep_checksums[dep] = ""

            # Combine checksums for overall comparison (include deployment context)
            combined_checksum = hashlib.sha256(
                (
                    "".join(current_dep_checksums.values())
                    + (self.bucket or "")
                    + (self.prefix_and_version or "")
                    + (self.region or "")
                ).encode()
            ).hexdigest()

            needs_rebuild = True
            changed_deps = []

            if os.path.exists(checksum_file):
                try:
                    with open(checksum_file, "r") as f:
                        stored_data = json.load(f)
                    stored_checksum = stored_data.get("combined", "")
                    stored_dep_checksums = stored_data.get("dependencies", {})

                    needs_rebuild = combined_checksum != stored_checksum

                    # Identify which specific dependencies changed
                    if needs_rebuild:
                        for dep, current_cs in current_dep_checksums.items():
                            stored_cs = stored_dep_checksums.get(dep, "")
                            if current_cs != stored_cs:
                                changed_deps.append(dep)
                except (json.JSONDecodeError, KeyError):
                    # Old format or corrupted - rebuild and show all deps
                    changed_deps = deps
            else:
                # No checksum file - show all deps as changed
                changed_deps = deps

            if needs_rebuild:
                components_to_rebuild.append(
                    {
                        "component": component,
                        "dependencies": deps,
                        "changed_dependencies": changed_deps,
                        "checksum_file": checksum_file,
                        "current_checksum": combined_checksum,
                        "current_dep_checksums": current_dep_checksums,
                    }
                )
                if component == "lib":  # update _is_lib_changed
                    self._is_lib_changed = True

                # Show only changed dependencies
                if changed_deps:
                    change_msg = (
                        "changed"
                        if len(changed_deps) < len(deps)
                        else "new/no previous build"
                    )
                    self.console.print(
                        f"[yellow]📝 {component} needs rebuild ({change_msg}):[/yellow]"
                    )
                    for dep in changed_deps:
                        self.console.print(f"[yellow]   • {dep}[/yellow]")

        return components_to_rebuild

    def clear_component_cache(self, component):
        """Clear build cache for a specific component.

        For main component, only clears the 'build' subdirectory to preserve
        the 'layers' subdirectory which contains Lambda layer zips.
        """
        if component == "main":
            # For main, only clear the build subdirectory, NOT the layers directory
            sam_build_dir = ".aws-sam/build"
            if os.path.exists(sam_build_dir):
                self.log_verbose(
                    f"Clearing SAM build cache for {component}: {sam_build_dir}"
                )
                try:
                    shutil.rmtree(sam_build_dir)
                except (FileNotFoundError, OSError) as e:
                    self.log_verbose(f"Warning: Error clearing SAM cache: {e}")
        else:
            sam_dir = os.path.join(component, ".aws-sam")
            if os.path.exists(sam_dir):
                self.log_verbose(
                    f"Clearing entire SAM cache for {component}: {sam_dir}"
                )
                try:
                    shutil.rmtree(sam_dir)
                except (FileNotFoundError, OSError) as e:
                    self.log_verbose(
                        f"Warning: Error clearing SAM cache (may already be deleted): {e}"
                    )
                    # Try alternative cleanup method for broken symlinks
                    try:
                        subprocess.run(["rm", "-rf", sam_dir], check=False)
                    except Exception as e2:
                        self.log_verbose(f"Alternative cleanup also failed: {e2}")

    def _validate_python_syntax(self, directory):
        """Validate Python syntax in all .py files in the directory"""

        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith(".py"):
                    file_path = os.path.join(root, file)
                    try:
                        py_compile.compile(file_path, doraise=True)
                    except py_compile.PyCompileError as e:
                        self.console.print(
                            f"[red]❌ Python syntax error in {file_path}[/red]"
                        )
                        self.console.print(str(e), style="red", markup=False)
                        return False
        return True

    def _validate_python_linting(self):
        """Validate Python linting"""
        if not self.lint_enabled:
            return True

        self.console.print("[cyan]🔍 Running Python linting...[/cyan]")

        # Run ruff check (same as GitLab CI lint-cicd)
        result = subprocess.run(["ruff", "check"], capture_output=True, text=True)
        if result.returncode != 0:
            self.console.print("[red]❌ Ruff linting failed![/red]")
            self.console.print(result.stdout, style="red", markup=False)
            return False

        # Run ruff format check (same as GitLab CI lint-cicd)
        result = subprocess.run(
            ["ruff", "format", "--check"], capture_output=True, text=True
        )
        if result.returncode != 0:
            self.console.print("[red]❌ Code formatting check failed![/red]")
            self.console.print(result.stdout, style="red", markup=False)
            return False

        self.console.print("[green]✅ Python linting passed[/green]")
        return True

    def _validate_cfn_lint(self):
        """Validate CloudFormation templates with cfn-lint after build/package"""
        if not self.lint_enabled:
            return True

        self.console.print(
            "[cyan]🔍 Running CloudFormation linting (cfn-lint) on packaged templates...[/cyan]"
        )

        # Check if cfn-lint is installed
        if not shutil.which("cfn-lint"):
            self.console.print(
                "[yellow]⚠️  cfn-lint not installed, skipping CloudFormation linting[/yellow]"
            )
            self.console.print("[dim]Install with: pip install cfn-lint[/dim]")
            return True

        all_errors = []
        all_warnings = []

        # List of templates to lint (packaged templates after token replacement)
        templates_to_lint = []

        # In headless or govcloud mode, the main template still contains UI/AppSync/
        # CloudFront/Cognito resources that will be stripped by the headless/govcloud
        # transformer later in the publish flow. Linting them here always fails for
        # GovCloud regions because those resource types don't exist. Skip the main
        # template and any nested templates that contain headless-stripped resources —
        # the outer publish flow lints the generated idp-headless.yaml / idp-govcloud.yaml
        # separately, with region-aware checks.
        main_packaged = ".aws-sam/idp-main.yaml"
        if self.headless:
            headless_packaged = ".aws-sam/idp-headless.yaml"
            if os.path.exists(headless_packaged):
                templates_to_lint.append(("Headless template", headless_packaged))
            else:
                self.console.print(
                    "[dim]Skipping main template lint — headless transformation runs later.[/dim]"
                )
        elif self.govcloud:
            self.console.print(
                "[dim]Skipping main template lint — GovCloud transformation runs later.[/dim]"
            )
        elif os.path.exists(main_packaged):
            templates_to_lint.append(("Main template", main_packaged))

        # Nested templates (packaged versions)
        # In headless mode, skip nested templates that contain resources stripped by the
        # headless transformer (currently: nested/api-resolvers, which contains AWS::AppSync::*).
        headless_skip_nested = {"appsync"} if self.headless else set()
        nested_dir = "nested"
        if os.path.exists(nested_dir):
            for nested_name in os.listdir(nested_dir):
                if nested_name in headless_skip_nested:
                    continue
                nested_packaged = os.path.join(
                    nested_dir, nested_name, ".aws-sam", "packaged.yaml"
                )
                if os.path.exists(nested_packaged):
                    templates_to_lint.append((f"Nested/{nested_name}", nested_packaged))

        # Pattern templates (packaged versions)
        patterns_dir = "patterns"
        if os.path.exists(patterns_dir):
            for pattern_name in os.listdir(patterns_dir):
                pattern_packaged = os.path.join(
                    patterns_dir, pattern_name, ".aws-sam", "packaged.yaml"
                )
                if os.path.exists(pattern_packaged):
                    templates_to_lint.append(
                        (f"Patterns/{pattern_name}", pattern_packaged)
                    )

        if not templates_to_lint:
            self.console.print(
                "[yellow]⚠️  No packaged templates found to lint[/yellow]"
            )
            return True

        # Lint each template
        for template_name, template_path in templates_to_lint:
            self.log_verbose(f"Linting {template_name}: {template_path}")

            result = subprocess.run(
                ["cfn-lint", template_path], capture_output=True, text=True
            )

            if result.returncode != 0:
                output = result.stdout + result.stderr
                lines = output.strip().split("\n") if output.strip() else []

                # Separate errors from warnings
                # cfn-lint output lines start with the rule code (e.g., "E3003 ..." or
                # "W1030 ..."). Use regex matching so that rule codes elsewhere in the
                # message (e.g., ARN prefixes like "AWS::EC2::") don't cause
                # misclassification.
                import re

                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if re.match(r"^E\d{4}\b", stripped):
                        all_errors.append(f"[{template_name}] {line}")
                    elif re.match(r"^W\d{4}\b", stripped):
                        all_warnings.append(f"[{template_name}] {line}")

        # Report results
        if all_errors:
            self.console.print("[red]❌ CloudFormation linting found errors:[/red]")
            for line in all_errors[:10]:  # Show first 10 errors
                self.console.print(f"[red]  {line}[/red]")
            if len(all_errors) > 10:
                self.console.print(
                    f"[red]  ... and {len(all_errors) - 10} more errors[/red]"
                )
            return False

        if all_warnings:
            self.console.print(
                f"[yellow]⚠️  CloudFormation linting found {len(all_warnings)} warnings (continuing):[/yellow]"
            )
            for line in all_warnings[:5]:  # Show first 5 warnings
                self.console.print(f"[dim]  {line}[/dim]")
            if len(all_warnings) > 5:
                self.console.print(
                    f"[dim]  ... and {len(all_warnings) - 5} more warnings[/dim]"
                )

        self.console.print(
            f"[green]✅ CloudFormation linting passed ({len(templates_to_lint)} templates checked)[/green]"
        )
        return True

    def compute_directory_hash(self, directory):
        """Compute hash of actual directory contents for layer versioning."""
        if not os.path.exists(directory):
            return ""

        checksums = []
        for root, dirs, files in os.walk(directory):
            dirs.sort()  # Consistent ordering
            for file in sorted(files):
                file_path = os.path.join(root, file)
                if os.path.isfile(file_path):
                    # Include relative path and content in hash for accuracy
                    rel_path = os.path.relpath(file_path, directory)
                    file_hash = self.get_file_checksum(file_path)
                    checksums.append(f"{rel_path}:{file_hash}")

        combined = "\n".join(checksums)
        return hashlib.sha256(combined.encode()).hexdigest()[:8]

    def build_lambda_layer(self, layer_name, layer_extras):
        """Build a single Lambda layer with specified extras.

        The hash is computed from:
        1. Source code hash of lib/idp_common_pkg (to detect when source changes)
        2. Layer content hash AFTER removing boto packages (to verify installed content)

        This dual-hash approach ensures:
        - When source changes, old layer zips won't be reused (source hash differs)
        - Layer content is accurately reflected (content hash)

        Layer zip naming: idp-common-{name}-{source_hash[:8]}-{content_hash[:8]}.zip

        Args:
            layer_name: Name of the layer (e.g., 'base', 'reporting', 'agents')
            layer_extras: List of extras to install (e.g., ['docs_service', 'image'])

        Returns:
            Tuple of (layer_zip_path, layer_zip_name)
        """
        try:
            # Create layer directory structure
            layer_build_dir = os.path.join(".aws-sam", "layers", f"{layer_name}-build")
            layer_python_dir = os.path.join(layer_build_dir, "python")

            # Clean and recreate directories
            if os.path.exists(layer_build_dir):
                shutil.rmtree(layer_build_dir)
            os.makedirs(layer_python_dir, exist_ok=True)

            # Clean idp_common_pkg build artifacts to prevent stale .dist-info conflicts
            # (setuptools fails with "File exists" if old build/ dir has stale .dist-info)
            for clean_dir in [
                os.path.join("lib", "idp_common_pkg", "build"),
                os.path.join("lib", "idp_common_pkg", "dist"),
            ]:
                if os.path.exists(clean_dir):
                    shutil.rmtree(clean_dir)

            # Build pip install command with extras
            if layer_extras:
                extras_str = ",".join(layer_extras)
                install_spec = f"./lib/idp_common_pkg[{extras_str}]"
            else:
                install_spec = "./lib/idp_common_pkg"

            # Install dependencies into layer python directory using uv
            # Use platform-specific flags to ensure x86_64 Lambda compatibility
            # regardless of the local machine's architecture (e.g., ARM64 Mac)
            # Note: uv is used instead of pip because uv-created venvs don't include pip,
            # and uv handles package installation directly without needing pip in the venv.
            cmd = [
                "uv",
                "pip",
                "install",
                install_spec,
                "--python-platform",
                # manylinux_2_28 (glibc 2.28) matches the Lambda python3.12
                # runtime (Amazon Linux 2023, glibc 2.34). Required because
                # pyarrow >= 21 no longer ships manylinux2014 wheels, only
                # manylinux_2_28. This target still accepts older
                # manylinux2014 wheels for all other dependencies.
                "x86_64-manylinux_2_28",
                "--python-version",
                "3.12",
                "--only-binary=:all:",
                "--no-binary",
                "idp-common",
                "--target",
                layer_python_dir,
                "--upgrade",
            ]

            # Show what's being installed
            extras_info = (
                f" [{', '.join(layer_extras)}]" if layer_extras else " (core only)"
            )
            self.console.print(
                f"[cyan]Building layer '{layer_name}'{extras_info}...[/cyan]"
            )
            self.console.print(f"Installing: {install_spec}", style="dim", markup=False)

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise Exception(f"Layer build failed: {result.stderr}")

            # Copy idp_sdk to layers
            self.console.print("[cyan]  Copying idp_sdk package files...[/cyan]")
            sdk_source = "./lib/idp_sdk/idp_sdk"
            sdk_dest = os.path.join(layer_python_dir, "idp_sdk")
            if os.path.exists(sdk_dest):
                shutil.rmtree(sdk_dest)
            shutil.copytree(sdk_source, sdk_dest)

            # Remove Lambda runtime packages (already provided by Lambda runtime)
            # This saves ~100+ MB per layer and prevents size limit issues
            self.console.print(
                "[dim]  Removing packages already included in Lambda runtime (boto3, botocore, etc.)...[/dim]"
            )
            runtime_packages = [
                "boto3",
                "botocore",
                "s3transfer",
                "awscli",
                "urllib3",  # Included with botocore
                "jmespath",  # Included with botocore
                "python_dateutil",  # Included with botocore
                "dateutil",  # Included with botocore
            ]

            removed_packages = []
            for pkg in runtime_packages:
                # Remove package directories and dist-info directories
                for pattern in [pkg, f"{pkg}-*", f"{pkg.replace('-', '_')}-*"]:
                    import glob

                    matches = glob.glob(os.path.join(layer_python_dir, pattern))
                    for match in matches:
                        if os.path.isdir(match):
                            shutil.rmtree(match)
                            removed_packages.append(os.path.basename(match))
                        elif os.path.isfile(match):
                            os.remove(match)
                            removed_packages.append(os.path.basename(match))

            if removed_packages:
                self.log_verbose(
                    f"  Removed Lambda runtime packages: {', '.join(set(removed_packages))}"
                )

            # Compute SOURCE hash from both idp_common_pkg and idp_sdk
            common_hash = self.get_source_files_checksum("./lib/idp_common_pkg")[:8]
            sdk_hash = self.get_source_files_checksum("./lib/idp_sdk")[:8]
            source_hash = hashlib.sha256(
                f"{common_hash}{sdk_hash}".encode()
            ).hexdigest()[:8]

            layer_zip_name = f"idp-common-{layer_name}-{source_hash}.zip"
            layer_zip_path = os.path.join(".aws-sam", "layers", layer_zip_name)

            # Check if layer with this source hash already exists
            if os.path.exists(layer_zip_path):
                self.console.print(
                    f"[green]Layer {layer_name} already built with same source: {layer_zip_name}[/green]"
                )
                shutil.rmtree(layer_build_dir)
                return layer_zip_path, layer_zip_name

            # Create zip file
            self.console.print(f"[cyan]Creating layer zip: {layer_zip_name}[/cyan]")
            with zipfile.ZipFile(layer_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(layer_build_dir):
                    # Exclude unnecessary files
                    dirs[:] = [
                        d for d in dirs if d not in {"__pycache__", "*.dist-info"}
                    ]
                    for file in files:
                        if file.endswith((".pyc", ".pyo", ".dist-info")):
                            continue
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, layer_build_dir)
                        zipf.write(file_path, arcname)

            # Clean up build directory
            shutil.rmtree(layer_build_dir)

            layer_size_mb = os.path.getsize(layer_zip_path) / 1024 / 1024
            self.console.print(
                f"[green]✅ Layer '{layer_name}' built: {layer_size_mb:.2f} MB[/green]"
            )

            return layer_zip_path, layer_zip_name

        except Exception as e:
            self.console.print(f"[red]❌ Failed to build layer '{layer_name}':[/red]")
            self.console.print(str(e), style="red", markup=False)
            sys.exit(1)

    def _verify_packaged_templates_exist(self, components_needing_rebuild):
        """Verify that packaged templates exist for components NOT needing rebuild.

        If a component's checksum says it's up-to-date but the packaged.yaml is missing,
        add it to the rebuild list. This handles cases where .aws-sam/ was deleted
        but .checksum file still exists.
        """
        dependencies = self.get_component_dependencies()

        for component in dependencies.keys():
            if component in ["main", "lib"]:
                continue  # Main and lib don't have packaged templates

            # Check if component is already marked for rebuild
            already_marked = any(
                item["component"] == component for item in components_needing_rebuild
            )
            if already_marked:
                continue

            # Check if packaged.yaml exists
            packaged_path = os.path.join(component, ".aws-sam", "packaged.yaml")
            if not os.path.exists(packaged_path):
                self.console.print(
                    f"[yellow]⚠️  {component}/packaged.yaml missing - forcing rebuild[/yellow]"
                )

                # Get component's dependencies for rebuild info
                deps = dependencies.get(component, [])
                current_dep_checksums = {}
                for dep in deps:
                    if os.path.isfile(dep):
                        current_dep_checksums[dep] = self.get_file_checksum(dep)
                    elif os.path.isdir(dep):
                        current_dep_checksums[dep] = self.get_source_files_checksum(dep)

                combined_checksum = hashlib.sha256(
                    (
                        "".join(current_dep_checksums.values())
                        + (self.bucket or "")
                        + (self.prefix_and_version or "")
                        + (self.region or "")
                    ).encode()
                ).hexdigest()

                components_needing_rebuild.append(
                    {
                        "component": component,
                        "dependencies": deps,
                        "changed_dependencies": ["packaged.yaml missing"],
                        "checksum_file": f"{component}/.checksum",
                        "current_checksum": combined_checksum,
                        "current_dep_checksums": current_dep_checksums,
                    }
                )

    def _discover_existing_layer_zips(self):
        """Discover existing layer zips in .aws-sam/layers/ directory.

        Used when lib hasn't changed but we need to populate _layer_arns
        with the correct layer zip names for template token replacement.

        IMPORTANT: Verifies that the SOURCE HASH in the layer zip filename matches
        the current source code hash. If it doesn't match (source changed but checksum
        didn't detect it), this returns empty dict to force a rebuild.

        Also verifies that layers exist in S3 at the current version path.
        If a layer exists locally but not in S3 (e.g., VERSION changed), it uploads it.

        Returns:
            Dict mapping layer names to layer info dicts with zip_name, etc.
            Empty dict if source hash doesn't match (triggers rebuild)
        """
        layers_dir = ".aws-sam/layers"
        layer_info = {}

        self.console.print(
            f"[cyan]🔍 Discovering existing layer zips in {layers_dir}...[/cyan]"
        )

        if not os.path.exists(layers_dir):
            self.console.print(
                "[yellow]⚠️  Layers directory not found - cannot discover existing layers[/yellow]"
            )
            return layer_info

        # Compute current source hash to verify existing layers match current source
        current_source_hash = self.get_source_files_checksum("./lib/idp_common_pkg")[:8]
        self.console.print(
            f"[dim]   Current lib source hash: {current_source_hash}[/dim]"
        )

        # Find existing layer zips
        layer_zips = [
            f
            for f in os.listdir(layers_dir)
            if f.startswith("idp-common-") and f.endswith(".zip")
        ]

        self.console.print(
            f"[dim]   Found {len(layer_zips)} layer zip files: {layer_zips}[/dim]"
        )

        # Map each layer name to its zip file
        # NOTE: Keep in sync with layers_config in build_all_lambda_layers()
        expected_layers = [
            "base",
            # "evaluation" is disabled - not referenced by any Lambda, adds 50MB+ to build
            "reporting",
            "agents",
            "multi_document_discovery",
        ]
        for layer_name in expected_layers:
            # Find the zip for this layer (format: idp-common-{name}-{source_hash}.zip)
            # Match based on current source hash to ensure we use up-to-date layers
            expected_zip_name = f"idp-common-{layer_name}-{current_source_hash}.zip"

            if expected_zip_name in layer_zips:
                zip_name = expected_zip_name
                zip_path = os.path.join(layers_dir, zip_name)
                layer_hash = current_source_hash
                s3_key = f"{self.prefix_and_version}/layers/{zip_name}"

                # Verify layer exists in S3 at current version path
                try:
                    self.s3_client.head_object(Bucket=self.bucket, Key=s3_key)
                    self.console.print(
                        f"[green]   ✓ Layer '{layer_name}': {zip_name} (source hash matches, in S3)[/green]"
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] == "404":
                        # Layer exists locally but not in S3 - upload it
                        self.console.print(
                            f"[yellow]   ⚠️  Layer '{layer_name}' not in S3 at current version path - uploading[/yellow]"
                        )
                        self.upload_to_s3_with_timer(
                            zip_path, s3_key, f"layer '{layer_name}'"
                        )
                    else:
                        raise

                layer_info[layer_name] = {
                    "zip_path": zip_path,
                    "zip_name": zip_name,
                    "hash": layer_hash,
                    "s3_key": s3_key,
                }
            else:
                # Layer with matching source hash not found
                # Check if there's an OLD layer (different hash) - indicates source changed
                old_matching = [
                    z for z in layer_zips if f"idp-common-{layer_name}-" in z
                ]
                if old_matching:
                    old_hash = (
                        old_matching[0]
                        .replace(f"idp-common-{layer_name}-", "")
                        .replace(".zip", "")
                    )
                    self.console.print(
                        f"[yellow]⚠️  Layer '{layer_name}' has stale source hash ({old_hash} != {current_source_hash}) - needs rebuild[/yellow]"
                    )
                else:
                    self.console.print(
                        f"[yellow]⚠️  No existing layer zip found for '{layer_name}'[/yellow]"
                    )
                # Don't add to layer_info - will trigger rebuild

        if len(layer_info) == len(expected_layers):
            self.console.print(
                f"[green]✅ Discovered {len(layer_info)} existing layer zips with matching source hash[/green]"
            )
        else:
            self.console.print(
                f"[yellow]⚠️  Only {len(layer_info)}/{len(expected_layers)} layers have matching source hash - will rebuild[/yellow]"
            )

        return layer_info

    def _verify_layer_zips_exist(self):
        """Verify that all layer zip files exist locally.

        Returns True if any layer zips are missing, requiring a rebuild.
        This prevents the situation where lib/.checksum exists but layer zips were deleted.
        """
        layers_dir = ".aws-sam/layers"
        if not os.path.exists(layers_dir):
            self.console.print(
                "[yellow]⚠️  Layers directory missing - forcing layer rebuild[/yellow]"
            )
            return True  # Need rebuild

        # Check if any idp-common-*.zip files exist
        layer_zips = [
            f
            for f in os.listdir(layers_dir)
            if f.startswith("idp-common-") and f.endswith(".zip")
        ]
        if not layer_zips:
            self.console.print(
                "[yellow]⚠️  No layer zips found in .aws-sam/layers/ - forcing layer rebuild[/yellow]"
            )
            return True  # Need rebuild

        # We have at least some layer zips, check we have all expected layers
        # NOTE: Keep in sync with layers_config in build_all_lambda_layers()
        expected_layers = [
            "base",
            # "evaluation" is disabled - not referenced by any Lambda, adds 50MB+ to build
            "reporting",
            "agents",
            "multi_document_discovery",
        ]
        for layer_name in expected_layers:
            found = any(f"idp-common-{layer_name}-" in z for z in layer_zips)
            if not found:
                self.console.print(
                    f"[yellow]⚠️  Layer zip for '{layer_name}' missing - forcing layer rebuild[/yellow]"
                )
                return True  # Need rebuild

        return False  # All layers exist

    def build_all_lambda_layers(self):
        """Build all 3 Lambda layers for idp_common.

        Returns:
            Dict mapping layer names to (zip_path, zip_name, hash) tuples
        """
        self.log_phase("Building Lambda Layers", "📦")

        # Ensure layers directory exists
        os.makedirs(".aws-sam/layers", exist_ok=True)

        # Define the 4 layers
        layers_config = {
            "base": [
                "docs_service",
                "image",
            ],
            # "evaluation" layer is disabled:
            # - Not referenced by any Lambda in template.yaml or nested stacks
            # - Adds 50MB+ to layer size (stickler + scikit-learn + numpy)
            # - Lambdas needing evaluation install idp_common[evaluation] directly in function package
            # "evaluation": ["evaluation"],
            "reporting": ["reporting"],
            "agents": ["agents"],
            "multi_document_discovery": ["multi_document_discovery"],
        }

        built_layers = {}

        for layer_name, layer_extras in layers_config.items():
            # Build the layer (hash is computed from actual contents after removing boto packages)
            self.log_task(f"Building layer '{layer_name}' [{', '.join(layer_extras)}]")
            zip_path, zip_name = self.build_lambda_layer(layer_name, layer_extras)

            # Extract hash from zip_name (format: idp-common-{name}-{hash}.zip)
            layer_hash = zip_name.split("-")[-1].replace(".zip", "")

            # Upload to S3
            s3_key = f"{self.prefix_and_version}/layers/{zip_name}"
            try:
                self.s3_client.head_object(Bucket=self.bucket, Key=s3_key)
                self.log_cached(f"Layer '{layer_name}' already in S3: {zip_name}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    self.upload_to_s3_with_timer(
                        zip_path, s3_key, f"layer '{layer_name}'"
                    )
                else:
                    raise

            # Store layer info for template injection
            built_layers[layer_name] = {
                "zip_path": zip_path,
                "zip_name": zip_name,
                "hash": layer_hash,
                "s3_key": s3_key,
            }

        self.log_success("All Lambda layers built and uploaded")
        return built_layers

    def _delete_checksum_file(self, checksum_path):
        """Delete checksum file - handles both component paths and direct file paths"""
        if os.path.isdir(checksum_path):
            # If it's a directory, look for .checksum inside it
            checksum_file = os.path.join(checksum_path, ".checksum")
        else:
            # If it's already a file path, use it directly
            checksum_file = checksum_path

        if os.path.exists(checksum_file):
            os.remove(checksum_file)
            self.log_verbose(f"Deleted checksum file: {checksum_file}")

    def update_component_checksum(self, components_needing_rebuild):
        """Update checksum with individual dependency tracking"""
        for item in components_needing_rebuild:
            current_checksum = item["current_checksum"]
            current_dep_checksums = item["current_dep_checksums"]
            checksum_file = item["checksum_file"]

            # Store both combined checksum and individual dependency checksums
            checksum_data = {
                "combined": current_checksum,
                "dependencies": current_dep_checksums,
            }

            with open(os.path.join(".", checksum_file), "w") as f:
                json.dump(checksum_data, f, indent=2)
            self.log_verbose(f"Updated checksum for {item['component']}")

    def smart_rebuild_detection(self):
        self.console.print(
            "[cyan]🔍 Analyzing component dependencies for smart rebuilds...[/cyan]"
        )

        # Safety check: verify layer zips exist even if checksum says they're up to date
        layers_missing = self._verify_layer_zips_exist()
        if layers_missing:
            self._is_lib_changed = True  # Force layer rebuild

        components_to_rebuild = self.get_components_needing_rebuild()

        # Safety check: verify packaged.yaml files exist for components marked as up-to-date
        # This handles cases where .aws-sam/ was deleted but .checksum file still exists
        self._verify_packaged_templates_exist(components_to_rebuild)

        components_names = []
        for item in components_to_rebuild:
            components_names.append(item["component"])

        if not components_to_rebuild:
            self.console.print("[green]✅ No components need rebuilding[/green]")
            return []
        self.console.print(
            f"[yellow]📦 {len(components_to_rebuild)} components need rebuilding:[/yellow]"
        )
        self.console.print(f"   📚 Components: {', '.join(components_names)}")
        return components_to_rebuild

    def print_outputs(self):
        """Print final outputs using Rich table formatting"""

        # Generate S3 URL for the main template
        template_url = f"https://s3.{self.region}.amazonaws.com/{self.bucket}/{self.prefix}/{self.main_template}"

        # URL encode the template URL for use in the CloudFormation console URL
        encoded_template_url = quote(template_url, safe=":/?#[]@!$&'()*+,;=")
        launch_url = f"https://{self.region}.console.aws.amazon.com/cloudformation/home?region={self.region}#/stacks/create/review?templateURL={encoded_template_url}&stackName=IDP"

        # Display deployment information first
        self.console.print("\n[bold cyan]Deployment Information:[/bold cyan]")
        self.console.print(f"  • Region: [yellow]{self.region}[/yellow]")
        self.console.print(f"  • Bucket: [yellow]{self.bucket}[/yellow]")
        self.console.print(
            f"  • Template Path: [yellow]{self.prefix}/{self.main_template}[/yellow]"
        )
        self.console.print(
            f"  • Public Access: [yellow]{'Yes' if self.public else 'No'}[/yellow]"
        )

        # Set public ACLs if requested
        self.set_public_acls()

        # Display hyperlinks with complete URLs as the display text
        self.console.print("\n[bold green]Deployment Outputs[/bold green]")

        # 1-Click Launch hyperlink with full URL as display text
        self.console.print("\n[cyan]1-Click Launch (creates new stack):[/cyan]")
        launch_link = f"[link={launch_url}]{launch_url}[/link]"
        self.console.print(f"  {launch_link}")

        # Template URL hyperlink with full URL as display text
        self.console.print("\n[cyan]Template URL (for updating existing stack):[/cyan]")
        template_link = f"[link={template_url}]{template_url}[/link]"
        self.console.print(f"  {template_link}")

    def set_public_acls(self):
        """Set public read ACLs on all uploaded artifacts if public option is enabled"""
        if not self.public:
            return

        self.console.print(
            "[cyan]Setting public read ACLs on published artifacts...[/cyan]"
        )

        try:
            # Collect objects across BOTH artifact prefixes:
            #   1. `<prefix>/<version>/`  — the versioned main-stack artifacts
            #      (Lambda/layer code zips, config_library, sam-objects, etc.).
            #   2. `<prefix>/extensions/` — the VERSION-FREE sample-feature base
            #      written by _upload_sample_feature_artifacts. These live as a
            #      SIBLING of prefix_and_version (e.g. `idp/extensions/<id>/...`
            #      vs `idp/0.5.16/...`), so the versioned pass alone never
            #      reaches them. Without this a cross-account public deploy hits
            #      S3 403 (Access Denied) the moment CloudFormation fetches the
            #      extension's `extensions/<id>/template.yaml`.
            paginator = self.s3_client.get_paginator("list_objects_v2")
            acl_prefixes = [
                self.prefix_and_version,
                f"{self.prefix}/extensions",
            ]

            objects = []
            for acl_prefix in acl_prefixes:
                for page in paginator.paginate(Bucket=self.bucket, Prefix=acl_prefix):
                    if "Contents" in page:
                        objects.extend(page["Contents"])

            if not objects:
                self.console.print("[yellow]No objects found to set ACLs on[/yellow]")
                return

            total_files = len(objects)
            self.console.print(f"[cyan]Setting ACLs on {total_files} files...[/cyan]")

            for i, obj in enumerate(objects, 1):
                self.s3_client.put_object_acl(
                    Bucket=self.bucket, Key=obj["Key"], ACL="public-read"
                )
                if i % 10 == 0 or i == total_files:
                    self.console.print(
                        f"[cyan]Progress: {i}/{total_files} files processed[/cyan]"
                    )

            # Set ACL for main template files
            main_template_keys = [
                f"{self.prefix}/{self.main_template}",
                f"{self.prefix}/{self.main_template.replace('.yaml', f'_{self.version}.yaml')}",
            ]

            for key in main_template_keys:
                self.s3_client.head_object(Bucket=self.bucket, Key=key)
                self.s3_client.put_object_acl(
                    Bucket=self.bucket, Key=key, ACL="public-read"
                )

            self.console.print("[green]✅ Public ACLs set successfully[/green]")

        except Exception as e:
            raise Exception(f"Failed to set public ACLs: {str(e)}")

    def run(self, args):
        """Main execution method"""
        # Track overall timing
        overall_start_time = time.time()
        timing_breakdown = {}

        try:
            # Parse and validate parameters
            step_start = time.time()
            self.check_parameters(args)
            timing_breakdown["Parameter validation"] = time.time() - step_start

            # Check for interrupted build state at startup - recover from any previous crash
            step_start = time.time()
            self._prepare_for_build_at_start()
            timing_breakdown["Build state recovery"] = time.time() - step_start

            # Container deployment is now handled within this script

            # Set up environment
            step_start = time.time()
            self.setup_environment()
            timing_breakdown["Environment setup"] = time.time() - step_start

            # Check prerequisites
            step_start = time.time()
            self.check_prerequisites()
            timing_breakdown["Prerequisites check"] = time.time() - step_start

            # Validate Python linting if enabled
            step_start = time.time()
            if not self._validate_python_linting():
                raise Exception("Python linting validation failed")
            timing_breakdown["Python linting"] = time.time() - step_start

            # Set up S3 bucket
            step_start = time.time()
            self.setup_artifacts_bucket()
            timing_breakdown["S3 bucket setup"] = time.time() - step_start

            # Get AWS account ID (needed for ECR placeholder)
            if not self.account_id:
                if not self.sts_client:
                    self.sts_client = boto3.client("sts", region_name=self.region)
                self.account_id = self.sts_client.get_caller_identity()["Account"]

            # Perform smart rebuild detection and cache management
            step_start = time.time()
            components_needing_rebuild = self.smart_rebuild_detection()
            timing_breakdown["Smart rebuild detection"] = time.time() - step_start

            # Start UI validation early in parallel
            step_start = time.time()
            ui_validation_future, ui_executor = self.start_ui_validation_parallel()
            timing_breakdown["Start UI validation"] = time.time() - step_start

            # clear component cache
            step_start = time.time()
            for comp_info in components_needing_rebuild:
                if comp_info["component"] != "lib":  # lib doesnt have sam build
                    self.clear_component_cache(comp_info["component"])
            timing_breakdown["Clear component cache"] = time.time() - step_start

            # Build Lambda layers if lib has changed, otherwise discover existing layers
            if self._is_lib_changed:
                step_start = time.time()
                self._layer_arns = self.build_all_lambda_layers()
                timing_breakdown["Build & upload Lambda layers"] = (
                    time.time() - step_start
                )
            else:
                # Discover existing layer zips to get their names for template replacement
                self._layer_arns = self._discover_existing_layer_zips()

                # If discovery failed to find layers, force rebuild
                if not self._layer_arns or len(self._layer_arns) < 3:
                    self.console.print(
                        "[yellow]⚠️  Layer discovery incomplete - forcing layer rebuild[/yellow]"
                    )
                    self._layer_arns = self.build_all_lambda_layers()

            # Build patterns and options with smart detection
            self.console.print(
                "[bold cyan]Building components with smart dependency detection...[/bold cyan]"
            )
            concurrent_build_start = time.time()

            # Determine optimal number of workers
            if self.max_workers is None:
                # Auto-detect: SAM builds are I/O bound, so use 2x CPU count, capped at 8
                cpu_count = os.cpu_count() or 4
                self.max_workers = min(cpu_count * 2, 8)
                self.console.print(
                    f"[green]Auto-detected {self.max_workers} concurrent workers (CPUs: {cpu_count})[/green]"
                )

            # All pattern Docker images (Pattern-1, Pattern-2) are built during CloudFormation deployment via CodeBuild
            # CodeBuild will download source from S3 and build images - no pre-build required
            self.console.print(
                "\n[cyan]ℹ️  Pattern Docker images (Pattern-1/2) will be built during stack deployment via CodeBuild[/cyan]"
            )

            # Build nested and patterns concurrently (no dependencies on each other)
            self.console.print(
                "\n[bold yellow]🚀 Building Nested Stacks and Patterns Concurrently[/bold yellow]"
            )

            # Submit both category builds concurrently
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=2
            ) as category_executor:
                # Submit builds for both categories
                nested_future = category_executor.submit(
                    self.build_components_with_smart_detection,
                    components_needing_rebuild,
                    "nested",
                    self.max_workers,
                )
                patterns_future = category_executor.submit(
                    self.build_components_with_smart_detection,
                    components_needing_rebuild,
                    "patterns",
                    self.max_workers,
                )

                # Wait for both categories to complete and collect results
                nested_start = time.time()
                nested_success = nested_future.result()
                nested_time = time.time() - nested_start

                patterns_start = time.time()
                patterns_success = patterns_future.result()
                patterns_time = time.time() - patterns_start

            # Check if any category failed
            if not nested_success:
                self.print_error_summary()
                self.console.print(
                    "[red]❌ Error: Failed to build one or more nested stacks[/red]"
                )
                if not self.verbose:
                    self.console.print(
                        "[dim]Use --verbose flag for detailed error information[/dim]"
                    )
                sys.exit(1)

            if not patterns_success:
                self.print_error_summary()
                self.console.print(
                    "[red]❌ Error: Failed to build one or more patterns[/red]"
                )
                if not self.verbose:
                    self.console.print(
                        "[dim]Use --verbose flag for detailed error information[/dim]"
                    )
                sys.exit(1)

            total_build_time = time.time() - concurrent_build_start
            timing_breakdown["Concurrent builds (nested + patterns)"] = total_build_time
            self.console.print(
                f"\n[bold green]✅ Concurrent build completed in {total_build_time:.2f}s[/bold green]"
            )
            self.console.print(f"   [dim]• Nested: {nested_time:.2f}s[/dim]")
            self.console.print(f"   [dim]• Patterns: {patterns_time:.2f}s[/dim]")
            self.console.print(
                f"   [dim]• Wall-clock time saved by concurrency: {max(nested_time, patterns_time) - total_build_time:.2f}s[/dim]"
            )

            if components_needing_rebuild:
                # Upload configuration library
                step_start = time.time()
                self.upload_config_library()
                timing_breakdown["Upload config library"] = time.time() - step_start

            # Wait for UI validation to complete if it was started
            if ui_validation_future:
                step_start = time.time()
                try:
                    self.console.print(
                        "[cyan]⏳ Waiting for UI validation to complete...[/cyan]"
                    )
                    ui_validation_future.result()
                    self.console.print(
                        "[green]✅ UI validation completed successfully[/green]"
                    )
                except Exception as e:
                    self.console.print("[red]❌ UI validation failed:[/red]")
                    self.console.print(str(e), style="red", markup=False)
                    sys.exit(1)
                finally:
                    ui_executor.shutdown(wait=True)
                timing_breakdown["UI validation (wait)"] = time.time() - step_start

            # Package UI and start validation in parallel if needed
            step_start = time.time()
            webui_zipfile = self.package_ui()
            timing_breakdown["Package UI"] = time.time() - step_start

            # Package unified pattern source for CodeBuild Docker builds
            step_start = time.time()
            unified_source_zipfile = self.package_unified_source()
            timing_breakdown["Package unified source"] = time.time() - step_start

            # Package multi-doc discovery source for CodeBuild Docker builds
            step_start = time.time()
            self.package_multi_doc_discovery_source()
            timing_breakdown["Package multi-doc discovery source"] = (
                time.time() - step_start
            )

            # Build + package the Feature Platform plumbing nested stack so the
            # `feature-platform/main-stack-extensions/.aws-sam/packaged.yaml`
            # URL the main template references resolves. It carries its own
            # parameters (not "nested"/"patterns"), so it is built here rather
            # than via the category-filtered concurrent builder.
            fp_plumbing_dir = "feature-platform/main-stack-extensions"
            if os.path.isdir(fp_plumbing_dir):
                step_start = time.time()
                self.build_and_package_template(fp_plumbing_dir, force_rebuild=True)
                timing_breakdown["Build feature-platform plumbing"] = (
                    time.time() - step_start
                )

            # Build + upload bundled sample feature(s) to the artifact bucket
            # so the feature platform's catalog is pre-populated at deploy time.
            # Returns empty when no bundled feature dirs exist (trimmed checkout).
            step_start = time.time()
            sample_features_hash, sample_features_list, oss_catalog_entries = (
                self.build_and_upload_sample_features()
            )
            timing_breakdown["Build & upload sample features"] = (
                time.time() - step_start
            )

            # Write config_library/catalog.json (OSS bundled features +
            # curated extensions-marketplace.yaml) and upload it to the
            # artifacts bucket's config_library/ prefix. The deploy-time
            # ConfigurationCopyFunction then copies it into the stack's own
            # ConfigurationBucket, where the host reads it at runtime — the
            # deployed stack never depends on the artifacts bucket for the
            # catalog, and never lists any bucket.
            step_start = time.time()
            self.write_catalog_file(oss_catalog_entries)
            self._upload_catalog_to_artifacts()
            timing_breakdown["Write & upload catalog.json"] = time.time() - step_start

            # Self-updating sample-document manifest (config_library/samples-manifest.json),
            # generated by scanning samples/. Rides the same config_library copy
            # into the ConfigurationBucket; the Quick Start agent's
            # list_sample_documents tool reads it at runtime.
            step_start = time.time()
            self.generate_samples_manifest()
            self._upload_samples_manifest_to_artifacts()
            timing_breakdown["Write & upload samples-manifest.json"] = (
                time.time() - step_start
            )

            # Upload the curated sample-document binaries to the artifacts
            # bucket. The deploy-time CopySampleFiles custom resource copies
            # them into the stack's ConfigurationBucket under samples/, matching
            # the samples-manifest s3Key values so the UI can launch them.
            step_start = time.time()
            self.upload_samples()
            timing_breakdown["Upload sample documents"] = time.time() - step_start

            # Build main template
            step_start = time.time()
            self.build_main_template(
                webui_zipfile,
                unified_source_zipfile,
                components_needing_rebuild,
                sample_features_hash=sample_features_hash,
                sample_features_list=sample_features_list,
            )
            timing_breakdown["Build & upload main template"] = time.time() - step_start

            # Validate CloudFormation templates with cfn-lint (after all templates are built/packaged)
            step_start = time.time()
            if not self._validate_cfn_lint():
                raise Exception("CloudFormation linting validation failed")
            timing_breakdown["CloudFormation linting"] = time.time() - step_start

            # All builds completed successfully if we reach here
            self.console.print("[green]✅ All builds completed successfully[/green]")

            # Update checksum for components needing rebuild upon success
            step_start = time.time()
            self.update_component_checksum(components_needing_rebuild)
            timing_breakdown["Update checksums"] = time.time() - step_start

            # Print outputs
            step_start = time.time()
            self.print_outputs()
            timing_breakdown["Print outputs"] = time.time() - step_start

            # Calculate total time
            total_time = time.time() - overall_start_time

            # Print timing breakdown - show top 4 steps and "Other"
            self.console.print("\n[bold cyan]⏱️  Timing Breakdown:[/bold cyan]")
            self.console.print("=" * 60)

            # Sort by duration (longest first)
            sorted_steps = sorted(
                timing_breakdown.items(), key=lambda x: x[1], reverse=True
            )

            # Show top 4 steps
            top_steps = sorted_steps[:4]
            for step_name, duration in top_steps:
                percentage = (duration / total_time * 100) if total_time > 0 else 0
                self.console.print(
                    f"  • {step_name:<40} {duration:>6.2f}s ({percentage:>5.1f}%)"
                )

            # Combine remaining steps as "Other"
            if len(sorted_steps) > 4:
                other_time = sum(duration for _, duration in sorted_steps[4:])
                other_percentage = (
                    (other_time / total_time * 100) if total_time > 0 else 0
                )
                self.console.print(
                    f"  • {'Other':<40} {other_time:>6.2f}s ({other_percentage:>5.1f}%)"
                )

            self.console.print("=" * 60)
            self.console.print(
                f"  [bold green]TOTAL TIME: {total_time:.2f}s ({total_time / 60:.1f} minutes)[/bold green]"
            )

            self.console.print("\n[bold green]✅ Done![/bold green]")

        except KeyboardInterrupt:
            self.console.print("\n[yellow]Operation cancelled by user[/yellow]")
            sys.exit(1)
        except Exception as e:
            self.console.print("[red]Error:[/red]")
            self.console.print(str(e), style="red", markup=False)
            import traceback

            self.console.print("\n[yellow]Traceback:[/yellow]")
            traceback.print_exc()
            sys.exit(1)


# No __main__ block — this is a library module.
# Use `idp-cli publish` or `python publish.py` (the backward-compat wrapper) instead.
