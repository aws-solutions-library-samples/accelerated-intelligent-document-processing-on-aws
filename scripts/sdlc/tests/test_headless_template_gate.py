"""Unit tests for the CI gate that validates the `--headless` template variant.

`validate_headless_template` is the pipeline's only check on the `--headless`
transform against REAL CloudFormation. Nothing in CI exercised that transform
before it: every deployment-variant probe deploys the STANDARD template with
different parameters (`jobsapi` was once named "headless" but tests the
EnableJobsApi *parameter*, not the transform), so a template CloudFormation
rejects outright shipped and broke every headless deploy for six weeks.

These tests use no AWS and no subprocess — boto3 and the transformer are
monkeypatched. They pin the properties the gate exists for:

  * a ValidateTemplate rejection is reported as a FAILURE (the whole point),
  * it reuses the already-packaged template rather than rebuilding,
  * bucket/prefix/region are derived from the published main-template URL so the
    headless artifact lands beside it,
  * it NEVER raises — a failure must reach the verdict, not crash the harness
    before the primary suite runs.
"""

import os

import pytest

import codebuild_deployment as cbd

pytestmark = pytest.mark.unit

MAIN_URL = "https://s3.us-west-2.amazonaws.com/my-bucket/codebuild-20260828-120000/idp-main.yaml"


class _FakeS3:
    def __init__(self, sink):
        self.sink = sink

    def upload_file(self, path, bucket, key, ExtraArgs=None):  # noqa: N803
        self.sink.append({"path": path, "bucket": bucket, "key": key})


class _FakeCfn:
    def __init__(self, error=None, sink=None):
        self.error = error
        self.sink = sink if sink is not None else []

    def validate_template(self, TemplateURL):  # noqa: N803
        self.sink.append(TemplateURL)
        if self.error:
            raise self.error


@pytest.fixture
def packaged_template(tmp_path, monkeypatch):
    """A .aws-sam/idp-main.yaml in the cwd, as the publish step leaves behind."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".aws-sam").mkdir()
    (tmp_path / ".aws-sam" / "idp-main.yaml").write_text("Resources: {}\n")
    return tmp_path


@pytest.fixture
def wired(monkeypatch):
    """Patch boto3 + the transformer; return the recorded calls."""
    calls = {"uploads": [], "validated": [], "transformed": []}

    def _fake_client(service, region_name=None):
        if service == "s3":
            return _FakeS3(calls["uploads"])
        if service == "cloudformation":
            return _FakeCfn(calls.get("cfn_error"), calls["validated"])
        raise AssertionError(f"unexpected client: {service}")

    monkeypatch.setattr(cbd.boto3, "client", _fake_client)

    class _FakeTransformer:
        def transform(self, src, dst):
            calls["transformed"].append((src, dst))
            with open(dst, "w") as fh:
                fh.write("Resources: {}\n")
            return calls.get("transform_ok", True)

    import idp_sdk._core.template_transform as tt

    monkeypatch.setattr(tt, "HeadlessTemplateTransformer", _FakeTransformer)
    return calls


def test_valid_headless_template_passes(packaged_template, wired):
    ok, detail = cbd.validate_headless_template(MAIN_URL)
    assert ok is True
    assert detail.endswith("/idp-headless.yaml")


def test_cloudformation_rejection_is_reported_as_failure(packaged_template, wired):
    """The regression this gate exists for: CFN rejects the transformed template."""
    wired["cfn_error"] = Exception(
        "An error occurred (ValidationError) when calling the ValidateTemplate "
        "operation: Template format error: Unresolved dependencies [AdminEmail]. "
        "Cannot reference resources in the Conditions block of the template"
    )
    ok, detail = cbd.validate_headless_template(MAIN_URL)
    assert ok is False
    assert "Unresolved dependencies [AdminEmail]" in detail


def test_reuses_the_packaged_template_and_does_not_rebuild(packaged_template, wired):
    """Must transform .aws-sam/idp-main.yaml — a second SAM build would cost ~1h."""
    cbd.validate_headless_template(MAIN_URL)
    src, dst = wired["transformed"][0]
    assert src == os.path.join(".aws-sam", "idp-main.yaml")
    assert dst == os.path.join(".aws-sam", "idp-headless.yaml")


def test_artifact_lands_beside_the_main_template(packaged_template, wired):
    """Bucket/prefix/region come from the published URL, not a recomputed prefix."""
    cbd.validate_headless_template(MAIN_URL)
    upload = wired["uploads"][0]
    assert upload["bucket"] == "my-bucket"
    assert upload["key"] == "codebuild-20260828-120000/idp-headless.yaml"
    assert wired["validated"] == [
        "https://s3.us-west-2.amazonaws.com/my-bucket/"
        "codebuild-20260828-120000/idp-headless.yaml"
    ]


def test_missing_packaged_template_fails_without_raising(tmp_path, monkeypatch, wired):
    monkeypatch.chdir(tmp_path)
    ok, detail = cbd.validate_headless_template(MAIN_URL)
    assert ok is False
    assert "packaged template not found" in detail


def test_transform_failure_fails_without_raising(packaged_template, wired):
    wired["transform_ok"] = False
    ok, detail = cbd.validate_headless_template(MAIN_URL)
    assert ok is False
    assert "transform reported failure" in detail


def test_upload_exception_fails_without_raising(packaged_template, monkeypatch):
    """Never raise: a failure must reach the verdict, not kill the harness."""

    def _boom(service, region_name=None):
        raise Exception("credentials expired")

    monkeypatch.setattr(cbd.boto3, "client", _boom)

    class _FakeTransformer:
        def transform(self, src, dst):
            with open(dst, "w") as fh:
                fh.write("Resources: {}\n")
            return True

    import idp_sdk._core.template_transform as tt

    monkeypatch.setattr(tt, "HeadlessTemplateTransformer", _FakeTransformer)

    ok, detail = cbd.validate_headless_template(MAIN_URL)
    assert ok is False
    assert "credentials expired" in detail


def test_unparseable_url_fails_without_raising(packaged_template, wired):
    ok, detail = cbd.validate_headless_template("https://s3.amazonaws.com/")
    assert ok is False
    assert "could not parse bucket/prefix" in detail
