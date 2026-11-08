# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Publish operations for IDP SDK.

Provides build, package, upload, and headless template generation capabilities.
This integrates the functionality of publish.py and generate_govcloud_template.py
into the SDK.
"""

import os
import re
import shutil
import subprocess
from typing import List, Optional, Tuple
from urllib.parse import quote

import boto3

from idp_sdk.exceptions import IDPConfigurationError, IDPStackError
from idp_sdk.models.publish import PublishResult, TemplateTransformResult

# Default GovCloud region to lint the transformed template against when the
# publish/deploy is being run from a commercial region (the common case: build
# in us-east-1, deploy the artifact into GovCloud). cfn-lint's per-region
# resource spec is what surfaces E3006 "resource type does not exist in
# <region>" for CloudFront / Lambda::Url, so the lint region must be a GovCloud
# region even when the build region is commercial.
DEFAULT_GOVCLOUD_LINT_REGION = "us-gov-west-1"


def _cfn_lint_region_errors(
    template_path: str, region: str
) -> Tuple[bool, List[str], str]:
    """Run region-aware cfn-lint on a template.

    Returns (ran, errors, note):
      - ran:    True if cfn-lint actually executed; False if it is not installed
                (caller decides whether a skip is acceptable).
      - errors: list of ``E####`` error lines (warnings are ignored here).
      - note:   human-readable status/skip message.

    cfn-lint is a fully offline static linter with a bundled per-region resource
    spec — no AWS credentials or network access are required. IMPORTANT: the
    template path MUST come before ``--region`` (``cfn-lint <file> --region
    <r>``); with the flag first, cfn-lint consumes the filename as a region value
    and errors out without linting.
    """
    if not shutil.which("cfn-lint"):
        return (False, [], "cfn-lint not installed — skipping GovCloud lint check")

    result = subprocess.run(
        ["cfn-lint", template_path, "--region", region],
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    errors = [
        line.strip()
        for line in output.splitlines()
        if re.match(r"^E\d{4}\b", line.strip())
    ]
    return (True, errors, f"cfn-lint ran for region {region}")


class PublishOperation:
    """Build, package, and publish IDP CloudFormation artifacts.

    This operation namespace consolidates the functionality of:
    - publish.py: Build Lambda functions, layers, SAM templates, and upload to S3
    - generate_govcloud_template.py: Transform templates for headless deployment

    Usage via SDK:
        client = IDPClient(region="us-east-1")
        result = client.publish.build(source_dir=".", bucket="my-bucket", prefix="my-prefix")

    Usage via CLI:
        idp-cli publish --source-dir . --bucket my-bucket --prefix my-prefix --region us-east-1
    """

    def __init__(self, client):
        self._client = client

    def build(
        self,
        source_dir: str,
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
        region: Optional[str] = None,
        *,
        headless: bool = False,
        govcloud: bool = False,
        public: bool = False,
        max_workers: Optional[int] = None,
        clean_build: bool = False,
        no_validate: bool = False,
        verbose: bool = False,
        lint: bool = True,
        terraform: bool = False,
    ) -> PublishResult:
        """Build and publish IDP CloudFormation artifacts to S3.

        This runs the full publish pipeline: build Lambda functions, Lambda layers,
        SAM templates, package UI, upload everything to S3. Optionally generates
        a headless template variant.

        Args:
            source_dir: Path to the IDP project root directory.
            bucket: S3 bucket basename for artifacts. If not provided, auto-generates
                    as ``idp-accelerator-artifacts-{account_id}``.
            prefix: S3 key prefix for artifacts. Default: ``idp-cli``.
            region: AWS region. Falls back to client region or boto3 default.
            headless: If True, also generate a headless (no-UI) template variant.
            govcloud: If True, also generate a GovCloud template variant that
                removes AWS::CloudFront::* resources and forces API Gateway
                hosting (keeps the full UI). For deployment in GovCloud regions.
            public: If True, make S3 artifacts publicly readable.
            max_workers: Maximum concurrent build workers. Default: auto-detect.
            clean_build: If True, delete all checksum files to force full rebuild.
            no_validate: If True, skip CloudFormation template validation.
            verbose: If True, enable verbose build output.
            lint: If True, enable ruff linting and cfn-lint. Default: True.
            terraform: If True, also translate the published main template to a
                Terraform module and upload it (see :meth:`publish_terraform`).

        Returns:
            PublishResult with template paths, URLs, and status.

        Raises:
            IDPConfigurationError: If source_dir doesn't contain publish.py.
            IDPStackError: If build fails.
        """
        region = region or self._client._region
        if not region:
            session = boto3.Session()
            region = session.region_name
        if not region:
            raise IDPConfigurationError(
                "Region is required for publish. Specify via --region, client region, "
                "or AWS_DEFAULT_REGION environment variable."
            )

        source_dir = os.path.abspath(source_dir)

        # Auto-generate bucket name if not provided
        if not bucket:
            sts = boto3.client("sts", region_name=region)
            account_id = sts.get_caller_identity()["Account"]
            bucket = f"idp-accelerator-artifacts-{account_id}"

        if not prefix:
            prefix = "idp-cli"

        # Build command-line arguments for publish.py
        publish_args = [bucket, prefix, region]
        if public:
            publish_args.append("public")
        if max_workers is not None:
            publish_args.extend(["--max-workers", str(max_workers)])
        if verbose:
            publish_args.append("--verbose")
        if no_validate or headless or govcloud:
            # Skip main-template validation when headless/govcloud — the base
            # template still contains resources (UI/CloudFront) that don't
            # validate in the target region; we validate the transformed
            # template instead after generating it.
            publish_args.append("--no-validate")
        if clean_build:
            publish_args.append("--clean-build")
        if not lint:
            publish_args.extend(["--lint", "off"])

        try:
            # Run IDPPublisher directly from the SDK's _core module
            from idp_sdk._core.publish import IDPPublisher

            original_cwd = os.getcwd()
            os.chdir(source_dir)
            try:
                publisher = IDPPublisher(verbose=verbose)
                publisher.headless = headless
                publisher.govcloud = govcloud
                publisher.run(publish_args)
            except SystemExit as e:
                if e.code != 0:
                    return PublishResult(
                        success=False,
                        error=f"Build failed with exit code {e.code}",
                    )
            finally:
                os.chdir(original_cwd)

            # Read VERSION for result
            version = ""
            version_file = os.path.join(source_dir, "VERSION")
            if os.path.exists(version_file):
                with open(version_file) as f:
                    version = f.read().strip()

            bucket_full = f"{bucket}-{region}"
            template_path = os.path.join(source_dir, ".aws-sam", "idp-main.yaml")
            template_url = f"https://s3.{region}.amazonaws.com/{bucket_full}/{prefix}/idp-main.yaml"

            headless_template_path = None
            headless_template_url = None
            govcloud_template_path = None
            govcloud_template_url = None

            # Generate GovCloud (CloudFront-free) template if requested. Keeps
            # the full Web UI but removes AWS::CloudFront::* resources and forces
            # API Gateway hosting (the only GovCloud-viable option).
            if govcloud:
                # Lint against the deploy region if it is already GovCloud;
                # otherwise (building from a commercial region) lint against the
                # default GovCloud region so E3006 checks still apply.
                lint_region = (
                    region
                    if region and region.startswith("us-gov-")
                    else DEFAULT_GOVCLOUD_LINT_REGION
                )
                govcloud_result = self.transform_template_govcloud(
                    source_template=template_path,
                    output_path=os.path.join(
                        source_dir, ".aws-sam", "idp-govcloud.yaml"
                    ),
                    verbose=verbose,
                    lint_region=lint_region,
                )
                if not govcloud_result.success:
                    return PublishResult(
                        success=False,
                        template_path=template_path,
                        template_url=template_url,
                        bucket=bucket_full,
                        prefix=prefix,
                        version=version,
                        error=(
                            "GovCloud template transformation failed: "
                            f"{govcloud_result.error}"
                        ),
                    )
                govcloud_template_path = govcloud_result.output_path
                s3_client = boto3.client("s3", region_name=region)
                gc_key = f"{prefix}/idp-govcloud.yaml"
                s3_client.upload_file(
                    govcloud_template_path,
                    bucket_full,
                    gc_key,
                    ExtraArgs={"ContentType": "text/yaml"},
                )
                govcloud_template_url = (
                    f"https://s3.{region}.amazonaws.com/{bucket_full}/{gc_key}"
                )
                if not no_validate:
                    try:
                        cf_client = boto3.client("cloudformation", region_name=region)
                        cf_client.validate_template(TemplateURL=govcloud_template_url)
                    except Exception as e:
                        return PublishResult(
                            success=False,
                            template_path=template_path,
                            template_url=template_url,
                            govcloud_template_path=govcloud_template_path,
                            govcloud_template_url=govcloud_template_url,
                            bucket=bucket_full,
                            prefix=prefix,
                            version=version,
                            error=f"GovCloud template validation failed: {e}",
                        )

            # Generate headless template if requested
            if headless:
                is_govcloud = region and region.startswith("us-gov-")
                headless_result = self.transform_template_headless(
                    source_template=template_path,
                    output_path=os.path.join(
                        source_dir, ".aws-sam", "idp-headless.yaml"
                    ),
                    update_govcloud_config=is_govcloud,
                )
                if headless_result.success and headless_result.output_path:
                    headless_template_path = headless_result.output_path

                    # Upload headless template to S3
                    s3_client = boto3.client("s3", region_name=region)
                    s3_key = f"{prefix}/idp-headless.yaml"
                    s3_client.upload_file(
                        headless_template_path,
                        bucket_full,
                        s3_key,
                        ExtraArgs={"ContentType": "text/yaml"},
                    )
                    headless_template_url = (
                        f"https://s3.{region}.amazonaws.com/{bucket_full}/{s3_key}"
                    )

                    # Validate headless template via CloudFormation API
                    if not no_validate:
                        try:
                            cf_client = boto3.client(
                                "cloudformation", region_name=region
                            )
                            cf_client.validate_template(
                                TemplateURL=headless_template_url
                            )
                        except Exception as e:
                            return PublishResult(
                                success=False,
                                template_path=template_path,
                                template_url=template_url,
                                headless_template_path=headless_template_path,
                                headless_template_url=headless_template_url,
                                bucket=bucket_full,
                                prefix=prefix,
                                version=version,
                                error=f"Headless template validation failed: {e}",
                            )

            terraform_info: dict = {}
            terraform_variants: dict = {}
            if terraform:
                # The standard module, then one per variant this run published.
                # A variant whose template this run did not upload is refused
                # rather than read: the key may hold an older publish's template.
                produced = {
                    "headless": headless_template_url,
                    "govcloud": govcloud_template_url,
                }
                wanted = [None] + [
                    v
                    for v, on in (("headless", headless), ("govcloud", govcloud))
                    if on
                ]
                for variant in wanted:
                    label = f"{variant} " if variant else ""
                    try:
                        if variant and not produced[variant]:
                            raise IDPStackError(
                                f"the {variant} template was not published"
                            )
                        info = self.publish_terraform(
                            source_dir=source_dir,
                            bucket=bucket_full,
                            prefix=prefix,
                            region=region,
                            version=version,
                            public=public,
                            variant=variant,
                        )
                    except (IDPConfigurationError, IDPStackError) as e:
                        return PublishResult(
                            success=False,
                            template_path=template_path,
                            template_url=template_url,
                            headless_template_url=headless_template_url,
                            govcloud_template_url=govcloud_template_url,
                            bucket=bucket_full,
                            prefix=prefix,
                            version=version,
                            error=f"Terraform {label}module publish failed: {e}",
                        )
                    if variant:
                        terraform_variants[variant] = info
                    else:
                        terraform_info = info

            return PublishResult(
                success=True,
                terraform_module_url=terraform_info.get("moduleUrl"),
                terraform_module_sha256=terraform_info.get("sha256"),
                terraform_pointer_url=terraform_info.get("pointerUrl"),
                terraform_variant_modules=terraform_variants,
                template_path=template_path,
                template_url=template_url,
                headless_template_path=headless_template_path,
                headless_template_url=headless_template_url,
                govcloud_template_path=govcloud_template_path,
                govcloud_template_url=govcloud_template_url,
                bucket=bucket_full,
                prefix=prefix,
                version=version,
            )

        except Exception as e:
            raise IDPStackError(f"Publish failed: {e}") from e

    def publish_terraform(
        self,
        source_dir: str,
        bucket: str,
        prefix: str,
        region: str,
        version: str,
        *,
        public: bool = False,
        template_key: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> dict:
        """Translate a published main template to a Terraform module and upload it.

        The main template is read back from ``s3://<bucket>/<template_key>``
        (default ``<prefix>/idp-main.yaml``, or ``<prefix>/idp-<variant>.yaml``
        for a variant, where :meth:`build` puts them), and translated online:
        nested templates are fetched by their ``TemplateURL``. The module tree is
        zipped deterministically and uploaded to the same bucket and prefix as
        the templates::

            <prefix>/<version>/terraform/idp-terraform-<version>.zip
            <prefix>/<version>/terraform/idp-terraform-<version>.zip.sha256
            <prefix>/terraform/idp-terraform-latest.json

        A variant (``headless`` or ``govcloud``) is named in all three keys:
        ``idp-terraform-<variant>-<version>.zip`` and
        ``idp-terraform-<variant>-latest.json``.

        Args:
            source_dir: Source checkout holding ``terraform/translator``.
            bucket: Full artifact bucket name (with the region suffix).
            prefix: S3 key prefix the templates were published under.
            region: Region of the bucket.
            version: Published version (labels the deployment, names the zip).
            public: Apply ``public-read`` to the uploaded objects.
            template_key: Key of the published main template.
            variant: ``None`` for the standard template, or ``headless`` /
                ``govcloud``.

        Returns:
            ``{"moduleUrl", "sha256", "pointerUrl", "version"}``; ``moduleUrl``
            is an HTTPS S3 URL ending ``.zip``, usable as a module source.

        Raises:
            IDPConfigurationError: The translator is not in ``source_dir``.
            IDPStackError: Translation or upload failed.
        """
        from idp_sdk._core import terraform_deploy as tf

        tf.check_variant(variant)
        if not version:
            raise IDPConfigurationError("a version is required to publish a module")
        source_dir = os.path.abspath(source_dir)
        work = os.path.join(source_dir, ".aws-sam", "terraform")
        if variant:
            work = os.path.join(work, variant)
        os.makedirs(work, exist_ok=True)
        key = template_key or tf.variant_template_key(prefix, variant)
        local_template = os.path.join(work, os.path.basename(key))
        s3 = boto3.client("s3", region_name=region)
        try:
            s3.download_file(bucket, key, local_template)
        except Exception as e:
            raise IDPStackError(
                f"cannot read the published template s3://{bucket}/{key}: {e}"
            ) from e
        module_dir = tf.translate_published_template(
            source_dir, local_template, work, version
        )
        try:
            return tf.upload_module(
                s3,
                module_dir=module_dir,
                bucket=bucket,
                prefix=prefix,
                region=region,
                version=version,
                public=public,
                variant=variant,
            )
        except Exception as e:
            raise IDPStackError(f"Terraform module upload failed: {e}") from e

    def transform_template_headless(
        self,
        source_template: str,
        output_path: Optional[str] = None,
        *,
        update_govcloud_config: bool = False,
        verbose: bool = False,
    ) -> TemplateTransformResult:
        """Transform a CloudFormation template for headless (no-UI) deployment.

        Removes UI, AppSync, Cognito, WAF, Agent, HITL, and Knowledge Base
        resources from the template.

        Args:
            source_template: Path to the source CloudFormation YAML template.
            output_path: Path to write the headless template. If not provided,
                        appends '-headless' to the source filename.
            update_govcloud_config: If True, update configuration maps for GovCloud.
            verbose: If True, enable verbose logging.

        Returns:
            TemplateTransformResult with paths and status.
        """
        from idp_sdk._core.template_transform import HeadlessTemplateTransformer

        if not os.path.exists(source_template):
            return TemplateTransformResult(
                success=False,
                input_path=source_template,
                error=f"Source template not found: {source_template}",
            )

        if not output_path:
            base, ext = os.path.splitext(source_template)
            output_path = f"{base}-headless{ext}"

        try:
            transformer = HeadlessTemplateTransformer(verbose=verbose)
            success = transformer.transform(
                source_template,
                output_path,
                update_govcloud_config=update_govcloud_config,
            )

            if success:
                return TemplateTransformResult(
                    success=True,
                    input_path=source_template,
                    output_path=output_path,
                )
            else:
                return TemplateTransformResult(
                    success=False,
                    input_path=source_template,
                    output_path=output_path,
                    error="Template transformation failed validation",
                )

        except Exception as e:
            return TemplateTransformResult(
                success=False,
                input_path=source_template,
                error=str(e),
            )

    def transform_template_govcloud(
        self,
        source_template: str,
        output_path: Optional[str] = None,
        *,
        verbose: bool = False,
        lint_region: Optional[str] = DEFAULT_GOVCLOUD_LINT_REGION,
    ) -> TemplateTransformResult:
        """Transform a template for GovCloud (CloudFront-free, API-Gateway hosting).

        Unlike the headless transform, this KEEPS the full Web UI but removes the
        ``AWS::CloudFront::*`` resources (which do not exist in GovCloud regions
        and cause ``E3006`` errors) and forces ``WebUIHosting=APIGateway`` so the
        UI is served as an S3 proxy on the REST API.

        After the transform, the output template is linted with a region-aware
        ``cfn-lint`` (``lint_region``, default ``us-gov-west-1``) so that any
        remaining GovCloud-unsupported resource type (``E3006``) fails the
        transform loudly rather than surfacing only at deploy time. The lint is
        offline (no credentials). It is skipped gracefully if cfn-lint is not
        installed. Pass ``lint_region=None`` to disable the lint gate.

        Args:
            source_template: Path to the source CloudFormation YAML template.
            output_path: Path to write the transformed template. Defaults to
                appending ``-govcloud`` to the source filename.
            verbose: If True, enable verbose logging.
            lint_region: GovCloud region to lint the transformed template
                against, or None to skip the region-aware lint gate.

        Returns:
            TemplateTransformResult with paths and status.
        """
        from idp_sdk._core.template_transform import GovCloudTemplateTransformer

        if not os.path.exists(source_template):
            return TemplateTransformResult(
                success=False,
                input_path=source_template,
                error=f"Source template not found: {source_template}",
            )

        if not output_path:
            base, ext = os.path.splitext(source_template)
            output_path = f"{base}-govcloud{ext}"

        try:
            transformer = GovCloudTemplateTransformer(verbose=verbose)
            success = transformer.transform(source_template, output_path)
            if not success:
                return TemplateTransformResult(
                    success=False,
                    input_path=source_template,
                    output_path=output_path,
                    error="GovCloud template transformation failed validation",
                )

            # Region-aware cfn-lint gate: catch any GovCloud-unsupported resource
            # type (E3006) that survived the transform, against a GovCloud region.
            if lint_region:
                ran, errors, note = _cfn_lint_region_errors(output_path, lint_region)
                if ran and errors:
                    preview = "\n".join(errors[:10])
                    return TemplateTransformResult(
                        success=False,
                        input_path=source_template,
                        output_path=output_path,
                        error=(
                            f"GovCloud template still has cfn-lint errors for "
                            f"{lint_region} (a resource type unsupported in "
                            f"GovCloud likely survived the transform):\n{preview}"
                        ),
                    )

            return TemplateTransformResult(
                success=True,
                input_path=source_template,
                output_path=output_path,
            )
        except Exception as e:
            return TemplateTransformResult(
                success=False,
                input_path=source_template,
                error=str(e),
            )

    def print_deployment_urls(
        self,
        template_url: str,
        region: str,
        *,
        headless_template_url: Optional[str] = None,
        govcloud_template_url: Optional[str] = None,
        stack_name: str = "IDP",
        terraform_module_url: Optional[str] = None,
        terraform_pointer_url: Optional[str] = None,
        terraform_variant_modules: Optional[dict] = None,
    ) -> None:
        """Print deployment URLs and 1-click launch links.

        Args:
            template_url: S3 URL of the main template.
            region: AWS region.
            headless_template_url: S3 URL of the headless template (optional).
            govcloud_template_url: S3 URL of the GovCloud template (optional).
            stack_name: Default stack name for launch URL.
            terraform_module_url: Terraform module zip URL (optional).
            terraform_pointer_url: Terraform latest-pointer URL (optional).
            terraform_variant_modules: Variant name -> ``{"moduleUrl",
                "pointerUrl", ...}`` for the variant modules (optional).
        """
        if "us-gov" in region:
            domain = "amazonaws-us-gov.com"
        else:
            domain = "aws.amazon.com"

        encoded_url = quote(template_url, safe=":/?#[]@!$&'()*+,;=")
        launch_url = (
            f"https://{region}.console.{domain}/cloudformation/home?"
            f"region={region}#/stacks/create/review?"
            f"templateURL={encoded_url}&stackName={stack_name}"
        )

        print("\n📦 Template URL (for updating existing stack):")
        print(f"  {template_url}")
        print("\n🚀 1-Click Launch (creates new stack):")
        print(f"  {launch_url}")

        if headless_template_url:
            encoded_headless = quote(headless_template_url, safe=":/?#[]@!$&'()*+,;=")
            headless_launch_url = (
                f"https://{region}.console.{domain}/cloudformation/home?"
                f"region={region}#/stacks/create/review?"
                f"templateURL={encoded_headless}&stackName={stack_name}-Headless"
            )
            print("\n🔧 Headless Template URL:")
            print(f"  {headless_template_url}")
            print("\n🚀 Headless 1-Click Launch:")
            print(f"  {headless_launch_url}")

        if terraform_module_url:
            print("\n🧱 Terraform module source:")
            print(f"  {terraform_module_url}")
            if terraform_pointer_url:
                print("  Latest pointer:")
                print(f"  {terraform_pointer_url}")
        for variant, info in sorted((terraform_variant_modules or {}).items()):
            print(f"\n🧱 Terraform {variant} module source:")
            print(f"  {info.get('moduleUrl')}")
            if info.get("pointerUrl"):
                print("  Latest pointer:")
                print(f"  {info['pointerUrl']}")

        if govcloud_template_url:
            encoded_gc = quote(govcloud_template_url, safe=":/?#[]@!$&'()*+,;=")
            gc_launch_url = (
                f"https://{region}.console.{domain}/cloudformation/home?"
                f"region={region}#/stacks/create/review?"
                f"templateURL={encoded_gc}&stackName={stack_name}"
            )
            print("\n🏛️  GovCloud Template URL (CloudFront-free, API Gateway hosting):")
            print(f"  {govcloud_template_url}")
            print("\n🚀 GovCloud 1-Click Launch:")
            print(f"  {gc_launch_url}")
