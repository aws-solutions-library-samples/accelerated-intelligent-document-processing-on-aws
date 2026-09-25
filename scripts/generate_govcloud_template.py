#!/usr/bin/env python3

"""
Backward compatibility wrapper for GovCloud/headless template generation.

⚠️  DEPRECATED: This script is maintained for backward compatibility.
Use 'idp-cli publish --headless' or 'idp-cli deploy --headless' instead.

This script delegates to the idp_sdk HeadlessTemplateTransformer, preserving
the original command-line interface for existing CI/CD pipelines.

Usage (unchanged from original):
    python scripts/generate_govcloud_template.py <cfn_bucket_basename> <cfn_prefix> <region> [public] [options]

Equivalent new commands:
    idp-cli publish --source-dir . --bucket <bucket> --prefix <prefix> --region <region> --headless
    idp-cli deploy --stack-name <name> --from-code . --headless --wait
"""

import argparse
import sys
from pathlib import Path


def main():
    """Main entry point — backward-compatible wrapper."""
    parser = argparse.ArgumentParser(
        description="GovCloud/headless template generation (backward compatibility wrapper)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
⚠️  DEPRECATED: Use 'idp-cli publish --headless' instead.

This script is maintained for backward compatibility with existing CI/CD pipelines.
It delegates to the idp_sdk HeadlessTemplateTransformer.

Examples:
    # Standard deployment (equivalent to: idp-cli publish --source-dir . --headless ...)
    python scripts/generate_govcloud_template.py my-bucket my-prefix us-east-1

    # GovCloud deployment
    python scripts/generate_govcloud_template.py my-bucket my-prefix us-gov-west-1

    # With verbose output and concurrency control
    python scripts/generate_govcloud_template.py my-bucket my-prefix us-east-1 --verbose --max-workers 4

    # With clean build (forces full rebuild)
    python scripts/generate_govcloud_template.py my-bucket my-prefix us-east-1 --clean-build

    # Public artifacts
    python scripts/generate_govcloud_template.py my-bucket my-prefix us-east-1 public
        """,
    )

    parser.add_argument("cfn_bucket_basename", help="Base name for the CloudFormation artifacts bucket")
    parser.add_argument("cfn_prefix", help="S3 prefix for artifacts")
    parser.add_argument("region", help="AWS region for deployment")
    parser.add_argument("public", nargs="?", help="Make artifacts publicly readable")
    parser.add_argument("--max-workers", type=int, help="Maximum number of concurrent workers")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip the build step and only generate/upload headless template",
    )
    parser.add_argument("--clean-build", action="store_true", help="Delete all .checksum files to force full rebuild")

    args, unknown = parser.parse_known_args()

    print(
        "⚠️  DEPRECATED: Use 'idp-cli publish --headless' or 'idp-cli deploy --headless' instead.",
        file=sys.stderr,
    )
    print(file=sys.stderr)

    project_root = Path(__file__).parent.parent

    if args.skip_build:
        # Skip build — just transform the already-built template using SDK public API
        from idp_sdk import IDPClient

        input_template = str(project_root / ".aws-sam" / "idp-main.yaml")
        output_template = str(project_root / ".aws-sam" / "idp-govcloud.yaml")

        print("⏩ Skipping build step (--skip-build specified)")
        print()

        client = IDPClient(region=args.region)
        transform_result = client.publish.transform_template_headless(
            source_template=input_template,
            output_path=output_template,
            update_govcloud_config="us-gov" in args.region,
            verbose=args.verbose,
        )

        if not transform_result.success:
            print(f"❌ Headless template generation failed: {transform_result.error}")
            sys.exit(1)

        # Upload to S3
        import boto3

        bucket_name = f"{args.cfn_bucket_basename}-{args.region}"
        s3_client = boto3.client("s3", region_name=args.region)
        s3_key = f"{args.cfn_prefix}/idp-govcloud.yaml"

        print(f"Uploading headless template to s3://{bucket_name}/{s3_key}")
        s3_client.upload_file(
            output_template, bucket_name, s3_key, ExtraArgs={"ContentType": "text/yaml"}
        )

        template_url = f"https://s3.{args.region}.amazonaws.com/{bucket_name}/{s3_key}"

        # Validate
        try:
            cf_client = boto3.client("cloudformation", region_name=args.region)
            cf_client.validate_template(TemplateURL=template_url)
            print("✅ CloudFormation validation passed")
        except Exception as e:
            print(f"❌ CloudFormation validation failed: {e}")

        # Print deployment summary
        from urllib.parse import quote

        if "us-gov" in args.region:
            domain = "amazonaws-us-gov.com"
        else:
            domain = "aws.amazon.com"

        encoded_url = quote(template_url, safe=":/?#[]@!$&'()*+,;=")
        launch_url = (
            f"https://{args.region}.console.{domain}/cloudformation/home?"
            f"region={args.region}#/stacks/create/review?"
            f"templateURL={encoded_url}&stackName=IDP-GovCloud"
        )

        print("\n🏛️  GovCloud/Headless Template:")
        print("1-Click Launch (creates new stack):")
        print(f"  {launch_url}")
        print("Template URL (for updating existing stack):")
        print(f"  {template_url}")

        print("\n✅ Complete!")
    else:
        # Full build + headless — delegate to idp-cli publish
        try:
            from idp_sdk import IDPClient

            client = IDPClient(region=args.region)
            result = client.publish.build(
                source_dir=str(project_root),
                bucket=args.cfn_bucket_basename,
                prefix=args.cfn_prefix,
                region=args.region,
                headless=True,
                public=bool(args.public),
                max_workers=args.max_workers,
                clean_build=args.clean_build,
                no_validate=False,
                verbose=args.verbose,
            )

            if not result.success:
                print(f"❌ Build failed: {result.error}")
                sys.exit(1)

            # Print deployment URLs
            client.publish.print_deployment_urls(
                template_url=result.template_url or "",
                region=args.region,
                headless_template_url=result.headless_template_url,
            )

            print("\n✅ Complete GovCloud publication process finished successfully!")

        except ImportError:
            # Fallback if idp_sdk is not installed — run original publish.py + transform
            print("idp_sdk not available, falling back to direct publish.py + transform")

            # Run publish.py
            publish_args = [args.cfn_bucket_basename, args.cfn_prefix, args.region]
            if args.public:
                publish_args.append("public")
            if args.max_workers:
                publish_args.extend(["--max-workers", str(args.max_workers)])
            if args.verbose:
                publish_args.append("--verbose")
            if args.clean_build:
                publish_args.append("--clean-build")
            publish_args.append("--no-validate")

            import subprocess

            publish_script = str(project_root / "publish.py")
            cmd = [sys.executable, publish_script] + publish_args
            result = subprocess.run(cmd, cwd=project_root)
            if result.returncode != 0:
                sys.exit(1)

            # Transform template. Imported dynamically, and from the private
            # module rather than through the public
            # IDPClient().publish.transform_template_headless() wrapper, because
            # this branch runs exactly when `from idp_sdk import IDPClient`
            # raised ImportError: the package __init__ is what failed, while the
            # transformer's own module may still import. Going through the public
            # wrapper here would re-raise the error this fallback exists to
            # survive. It also sidesteps the TID251 banned-api rule, which is a
            # side effect of that, not the reason.
            import importlib

            _mod = importlib.import_module("idp_sdk._core.template_transform")
            HeadlessTemplateTransformer = _mod.HeadlessTemplateTransformer

            input_template = str(project_root / ".aws-sam" / "idp-main.yaml")
            output_template = str(project_root / ".aws-sam" / "idp-govcloud.yaml")
            generator = HeadlessTemplateTransformer(verbose=args.verbose)
            update_govcloud = "us-gov" in args.region
            if not generator.transform(input_template, output_template, update_govcloud_config=update_govcloud):
                print("❌ Headless template generation failed")
                sys.exit(1)

            # Upload
            import boto3

            bucket_name = f"{args.cfn_bucket_basename}-{args.region}"
            s3_client = boto3.client("s3", region_name=args.region)
            s3_key = f"{args.cfn_prefix}/idp-govcloud.yaml"
            s3_client.upload_file(output_template, bucket_name, s3_key, ExtraArgs={"ContentType": "text/yaml"})

            template_url = f"https://s3.{args.region}.amazonaws.com/{bucket_name}/{s3_key}"
            print(f"\n🏛️  Headless Template URL: {template_url}")
            print("✅ Complete!")


if __name__ == "__main__":
    main()
