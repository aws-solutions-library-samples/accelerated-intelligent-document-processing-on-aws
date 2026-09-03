# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which S3 client the upload resolver uses for what.

`S3_ENDPOINT_URL` is the S3 interface VPC endpoint hostname, injected in private
deployments (``S3PresignedUrlViaVpcEndpoint=true`` or a BYO endpoint override).
It belongs in the URLs handed to the *browser* and nowhere else: this function is
not VPC-attached, so a VPCE hostname resolves to private addresses it cannot
route to, and an S3 API call aimed there hangs rather than failing. Behind the
29s REST API Gateway integration ceiling that reads to the user as a bodiless
`504`.

So the module keeps two clients, and these tests pin the difference:

* ``s3_presign_client`` — honours the endpoint (virtual-host addressing, so the
  SigV4 host header matches the VPCE DNS name).
* ``s3_client`` — never honours it; used for every real S3 call
  (``listSampleDocuments`` / ``uploadSampleDocument`` read the manifest, list
  objects and copy objects), with bounded timeouts so a misconfiguration is a
  fast logged error instead of a stall.
"""

import importlib
import os
import sys

import pytest

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../../../../nested/api-resolvers/src/lambda/upload_resolver",
    )
)

VPCE_ENDPOINT = "https://bucket.vpce-abc123.s3.us-east-1.vpce.amazonaws.com"


@pytest.fixture(autouse=True)
def _path_setup(monkeypatch):
    sys.path.insert(0, LAMBDA_DIR)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    yield
    sys.path.remove(LAMBDA_DIR)
    sys.modules.pop("index", None)


def _reload():
    if "index" in sys.modules:
        del sys.modules["index"]
    return importlib.import_module("index")


def _worst_case_seconds(cfg):
    """Longest a single S3 call can take, retries included.

    botocore normalizes ``retries={"max_attempts": N}`` (a RETRY count) into
    ``total_max_attempts = N + 1`` on the resolved config.
    """
    attempts = cfg.retries.get("total_max_attempts") or (
        cfg.retries["max_attempts"] + 1
    )
    return attempts * (cfg.connect_timeout + cfg.read_timeout)


class TestPresignClient:
    def test_public_mode_uses_path_addressing_and_no_endpoint(self, monkeypatch):
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        cfg = _reload().s3_presign_client.meta.config
        assert cfg.signature_version == "s3v4"
        assert cfg.s3["addressing_style"] == "path"

    def test_private_mode_uses_virtual_addressing_and_vpce_endpoint(self, monkeypatch):
        monkeypatch.setenv("S3_ENDPOINT_URL", VPCE_ENDPOINT)
        mod = _reload()
        cfg = mod.s3_presign_client.meta.config
        assert cfg.signature_version == "s3v4"
        assert cfg.s3["addressing_style"] == "virtual"
        assert mod.s3_presign_client.meta.endpoint_url == VPCE_ENDPOINT

    def test_empty_string_env_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("S3_ENDPOINT_URL", "")
        mod = _reload()
        assert mod.s3_presign_client.meta.config.s3["addressing_style"] == "path"
        assert mod.s3_presign_client.meta.endpoint_url.endswith("amazonaws.com")


class TestPublicBuildIsUnaffected:
    """No S3_ENDPOINT_URL is the default: public and GovCloud-without-BYO-VPCE.

    Both clients then resolve to ordinary S3 — what this resolver did before the
    split — asserted on the generated URL, the artifact the browser consumes.
    """

    def test_presigned_url_targets_ordinary_s3(self, monkeypatch):
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")  # nosec B105
        mod = _reload()

        post = mod.s3_presign_client.generate_presigned_post(
            Bucket="an-input-bucket", Key="doc.pdf", ExpiresIn=900
        )

        assert "vpce" not in post["url"]
        assert "amazonaws.com" in post["url"]

    def test_both_clients_agree_in_public_mode(self, monkeypatch):
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        mod = _reload()
        assert (
            mod.s3_presign_client.meta.endpoint_url == mod.s3_client.meta.endpoint_url
        )

    def test_no_timeout_bounds_are_applied(self, monkeypatch):
        """Stock botocore timeouts on the public path - see the sibling test."""
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        cfg = _reload().s3_client.meta.config
        assert cfg.connect_timeout == 60, (
            f"public build got connect_timeout={cfg.connect_timeout} instead of "
            "botocore's default 60 - the private-mode bound leaked onto the "
            "public path"
        )
        assert cfg.read_timeout == 60


class TestDataPlaneClient:
    def test_never_targets_the_vpc_endpoint(self, monkeypatch):
        """The regression. Data-plane calls must not inherit the VPCE host."""
        monkeypatch.setenv("S3_ENDPOINT_URL", VPCE_ENDPOINT)
        mod = _reload()
        assert mod.s3_client.meta.endpoint_url != VPCE_ENDPOINT
        assert mod.s3_client.meta.endpoint_url.endswith("amazonaws.com")
        assert "vpce" not in mod.s3_client.meta.endpoint_url

    def test_public_mode_unchanged(self, monkeypatch):
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        mod = _reload()
        cfg = mod.s3_client.meta.config
        assert cfg.signature_version == "s3v4"
        assert cfg.s3["addressing_style"] == "path"
        assert mod.s3_client.meta.endpoint_url.endswith("amazonaws.com")

    def test_timeouts_are_bounded_under_the_gateway_budget(self, monkeypatch):
        """Fail fast and visibly rather than stall into an unexplained 504."""
        monkeypatch.setenv("S3_ENDPOINT_URL", VPCE_ENDPOINT)
        cfg = _reload().s3_client.meta.config
        assert cfg.connect_timeout <= 5
        assert cfg.read_timeout <= 10
        worst_case = _worst_case_seconds(cfg)
        assert worst_case < 29, (
            f"worst-case S3 wait is {worst_case}s, which exceeds the 29s REST API "
            "Gateway integration ceiling — the caller would see a bodiless 504"
        )
