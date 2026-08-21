"""Shared fixtures for idp_feature_sdk tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _skip_when_sam_absent(monkeypatch):
    """Skip (not fail) any test that reaches the real `sam build`/`sam package`.

    FeaturePublisher.publish shells out to the SAM CLI to rewrite local CodeUri
    paths; PackPublisher delegates to the same method. Those tests are
    integration-level, but the suite runs in the offline `code_checks` fast gate
    (via `make test-packages-cicd`), which doesn't install SAM. Rather than
    hard-fail there, replace the sam step with a `pytest.skip` when the CLI is
    absent — so only tests that ACTUALLY invoke sam skip, while non-publishing
    tests (`--template-url`, mutex/error paths) still run. Where SAM is present
    (local `make test`, or any job that installs it) the real step runs
    unchanged. Future publish-path tests are covered automatically.
    """
    if shutil.which("sam") is not None:
        return
    from idp_feature_sdk.publisher import FeaturePublisher

    def _skip(*_args, **_kwargs):
        pytest.skip("requires the AWS SAM CLI (publish runs `sam build`)")

    monkeypatch.setattr(FeaturePublisher, "_sam_build_and_package", _skip)


def _bundle_body(feature_id: str, version: str) -> str:
    """Minimum bundle content that passes bundle.validate_bundle()."""
    return dedent(f"""
        (function(){{
            window.IdpFeatures.register('{feature_id}', {{
                Component: function(){{ return null; }},
                version: '{version}',
                displayName: 'demo',
            }});
        }})();
    """).strip()


@pytest.fixture
def demo_feature_project(tmp_path: Path) -> Path:
    """A minimal on-disk feature project that passes manifest + bundle validation."""
    root = tmp_path / "demo-feature"
    root.mkdir()

    # feature.yaml
    (root / "feature.yaml").write_text(
        dedent("""
            featureId: demo-feature
            displayName: Demo Feature
            version: 1.2.3
            template:
              path: template.yaml
              requiresMainStackName: true
            ui:
              bundlePath: feature-ui/dist/ui-bundle.js
            marketplace:
              productCode: prod-demo
              listingUrl: https://aws.amazon.com/marketplace/pp/prodview-XYZ
            defaultParameters:
              LogLevel: INFO
            capabilities:
              - custom-api
        """).strip(),
        encoding="utf-8",
    )

    # Template carries BOTH publish-time tokens so baking is exercised, and
    # declares the params the `deploy` command gates on (MainStackName,
    # FeatureBucket). The prefix token lands in Metadata (not a resource
    # property) so a baked value with slashes never trips resource validation.
    (root / "template.yaml").write_text(
        dedent("""
            AWSTemplateFormatVersion: '2010-09-09'
            Description: demo feat v<FEATURE_VERSION_TOKEN>
            Metadata:
              ArtifactPrefix: '<FEATURE_ARTIFACT_PREFIX_TOKEN>'
              ProductCode: '<FEATURE_PRODUCT_CODE_TOKEN>'
              ListingUrl: '<FEATURE_LISTING_URL_TOKEN>'
              LicenseMode: '<FEATURE_LICENSE_MODE_TOKEN>'
            Parameters:
              MainStackName:
                Type: String
              FeatureBucket:
                Type: String
                Default: <FEATURE_BUCKET_TOKEN>
            Resources:
              Dummy:
                Type: AWS::SNS::Topic
        """).strip()
        + "\n",
        encoding="utf-8",
    )
    (root / "feature-ui" / "dist").mkdir(parents=True)
    (root / "feature-ui" / "dist" / "ui-bundle.js").write_text(
        _bundle_body("demo-feature", "1.2.3"), encoding="utf-8"
    )
    return root


@pytest.fixture
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def feature_bucket(aws_credentials):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "test-feature-bucket"
        s3.create_bucket(Bucket=bucket)
        yield bucket
