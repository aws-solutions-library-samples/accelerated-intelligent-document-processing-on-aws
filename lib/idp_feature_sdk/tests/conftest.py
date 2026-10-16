"""Shared fixtures for idp_feature_sdk tests."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from textwrap import dedent

import aws_hermetic
import boto3
import pytest
from moto import mock_aws

#: Width to render Rich output at, for every test in this package.
#:
#: Rich decides its width from the output file's terminal size, falling back to 80 when
#: there is no terminal. Click's `CliRunner` captures into a StringIO, so there never is
#: one — and under `pytest -n` the worker has no controlling terminal either, while a
#: direct run inherits the developer's. So the same assertion passed serially and failed
#: under xdist: five tests asserting a long path, URL or account id found it wrapped
#: mid-token (`de\nfaulted`, `d eploy.yaml`) or replaced by a `…` inside a table.
#:
#: That is worse than a plain failure, because it makes the result a property of how
#: pytest was invoked rather than of the code. Pinning the width makes it deterministic
#: in both, and 200 is wide enough that nothing these tests assert wraps.
#:
#: Note whitespace-collapsing the output is NOT an adequate substitute: a table cell
#: truncated to `2026-08…` has lost characters that no amount of rejoining recovers.
RICH_TEST_WIDTH = "200"


# Set at conftest IMPORT time, not in a fixture. `idp_feature_sdk.cli` builds its
# `Console` at module level, and Rich captures the environment mapping it will consult
# when the Console is constructed — so a fixture that sets COLUMNS later has already
# missed it. pytest imports conftest before the test modules that import the CLI, which
# makes this the last point that is still early enough.
os.environ["COLUMNS"] = RICH_TEST_WIDTH


@pytest.fixture(autouse=True)
def _hermetic_aws(monkeypatch):
    """Give every test in this package the AWS environment a CI runner has.

    Autouse rather than opt-in, because the tests that most needed it were the
    ones that asked for no AWS fixture at all: `apply_public_artifacts_policy`
    is handed a stub S3 client by a test with no moto and no credentials in
    sight, and the bare `boto3.client(...)` it used to build inside signed a
    request to the public STS endpoint with whatever the machine could resolve.
    An opt-in fixture cannot reach a call a test does not know it is making.

    What this takes away, and why clearing the environment variables is not
    enough on its own, is written out in `aws_hermetic`.
    """
    aws_hermetic.apply(monkeypatch)
    yield
    # Also on the way out: a session built under the placeholders above must not
    # outlive them and be reused by whatever runs next in this worker.
    aws_hermetic.reset_default_session()


@pytest.fixture(autouse=True)
def _no_outbound_http(monkeypatch):
    """Fail loudly if a test reaches the network, naming the URL it tried.

    The fixture above makes the credentials harmless; this makes the request not
    happen. Both are wanted, because they fail in different directions: a
    placeholder credential still produces a signed request to a real endpoint,
    and a credential-neutralising fixture that is one day edited wrongly goes
    quiet rather than red.

    `lib/idp_cli_pkg/tests/conftest.py` carries the same fixture, and its
    reasoning applies here unchanged — this package publishes artifacts, writes
    bucket policies and creates and updates CloudFormation stacks, so a test
    that builds a real client by accident does not fail, it succeeds against
    whatever account is reachable.

    The seam is `botocore.httpsession.URLLib3Session.send`, the single point
    every botocore request passes through on its way out. `moto` short-circuits
    earlier, on botocore's `before-send` event, so a `mock_aws` test never
    reaches this and needs no exemption; a hand-rolled stub client never reaches
    it either. What does reach it is exactly the mistake worth catching.
    """
    from botocore.httpsession import URLLib3Session

    def _refuse(self, request):
        raise RuntimeError(
            "This test attempted a real network request to "
            f"{getattr(request, 'url', '<unknown>')}. Use moto (mock_aws) or "
            "patch the client; see the _no_outbound_http fixture in "
            "tests/conftest.py."
        )

    monkeypatch.setattr(URLLib3Session, "send", _refuse)


@pytest.fixture(autouse=True)
def _skip_when_sam_absent(monkeypatch):
    """Skip (not fail) any test that reaches the real `sam build`/`sam package`.

    FeaturePublisher.publish shells out to the SAM CLI to rewrite local CodeUri
    paths; PackPublisher delegates to the same method. Those tests are
    integration-level, but the suite runs in the offline `package_tests` fast gate
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


FIRST_PARTY_UNDER_TEST = ("idp_feature_sdk",)


# Fail fast if a first-party package resolves outside this checkout. These tests import
# the library as an external dependency, so nothing puts its source on `sys.path` and the
# editable-install pointer alone decides which revision runs. On a machine sharing one
# interpreter between checkouts that pointer is rewritten by whoever last ran
# `make test-cicd`, and the wrong tree produces a green run describing other code.
#
# The imports sit inside the function deliberately: this block is appended after a
# module's own imports, and module-level imports there are an E402 lint failure.
#
# Rationale, and the identity-vs-ancestry distinction:
# scripts/tests/first_party_provenance.py
def _assert_first_party_provenance(packages):
    import pathlib as _pathlib
    import sys as _sys

    for ancestor in _pathlib.Path(__file__).resolve().parents:
        gate = ancestor / "scripts" / "tests" / "first_party_provenance.py"
        if not gate.is_file():
            continue
        _sys.path.insert(0, str(gate.parent))
        try:
            from first_party_provenance import assert_resolves_in

            for package in packages:
                assert_resolves_in(package, __file__)
        finally:
            _sys.path.pop(0)
        return


_assert_first_party_provenance(FIRST_PARTY_UNDER_TEST)
