# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which S3 client the test-set resolver uses for what.

This is the resolver behind Test Studio, and the one where conflating the two
clients actually broke a deployment: ``getTestSets`` lists the whole test-set
bucket plus roughly four more calls per prefix on every poll. With
``S3_ENDPOINT_URL`` (the S3 interface VPC endpoint hostname, injected when
``S3PresignedUrlViaVpcEndpoint=true`` or a BYO override is set) applied to those
calls from a function that is not VPC-attached, each one hangs on an
unroutable private address; REST API Gateway abandons the integration at 29s and
the browser gets ``POST .../op/getTestSets 504`` with no body.

Presigning is the one legitimate use of that endpoint — it is offline signing,
and the resulting URL is for the browser, which *is* inside the network.
"""

import importlib
import os
import sys

import pytest

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../../../../nested/api-resolvers/src/lambda/test_set_resolver",
    )
)

VPCE_ENDPOINT = "https://bucket.vpce-tst.s3.us-east-1.vpce.amazonaws.com"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    sys.path.insert(0, LAMBDA_DIR)
    monkeypatch.setenv("TRACKING_TABLE", "dummy-table")
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
    ``total_max_attempts = N + 1`` on the resolved config, so read the
    normalized key and fall back for older botocore.
    """
    attempts = cfg.retries.get("total_max_attempts") or (
        cfg.retries["max_attempts"] + 1
    )
    return attempts * (cfg.connect_timeout + cfg.read_timeout)


class TestPresignClient:
    def test_public_mode(self, monkeypatch):
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        assert _reload().s3_presign_client.meta.config.s3["addressing_style"] == "path"

    def test_private_mode_keeps_the_vpc_endpoint(self, monkeypatch):
        monkeypatch.setenv("S3_ENDPOINT_URL", VPCE_ENDPOINT)
        mod = _reload()
        assert mod.s3_presign_client.meta.config.s3["addressing_style"] == "virtual"
        assert mod.s3_presign_client.meta.endpoint_url == VPCE_ENDPOINT


class TestPublicBuildIsUnaffected:
    """No S3_ENDPOINT_URL is the default: public and GovCloud-without-BYO-VPCE.

    Both clients then resolve to ordinary S3, which is what this resolver did
    before the split — asserted on the generated URL rather than on config, since
    the URL is the artifact the browser consumes.
    """

    def test_presigned_url_targets_ordinary_s3(self, monkeypatch):
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")  # nosec B105
        mod = _reload()

        post = mod.s3_presign_client.generate_presigned_post(
            Bucket="a-test-set-bucket", Key="set/x.zip", ExpiresIn=900
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
        """The public path keeps botocore's stock timeouts.

        The bounds exist to catch an unreachable S3 VPC endpoint. A public
        deployment has none, so applying them there would add a new way to fail
        without guarding anything. Pinned so the private-mode hardening cannot
        drift onto this path.
        """
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
        """The getTestSets 504 regression."""
        monkeypatch.setenv("S3_ENDPOINT_URL", VPCE_ENDPOINT)
        mod = _reload()
        assert mod.s3_client.meta.endpoint_url != VPCE_ENDPOINT
        assert "vpce" not in mod.s3_client.meta.endpoint_url
        assert mod.s3_client.meta.endpoint_url.endswith("amazonaws.com")

    def test_public_mode_unchanged(self, monkeypatch):
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        cfg = _reload().s3_client.meta.config
        assert cfg.signature_version == "s3v4"
        assert cfg.s3["addressing_style"] == "path"

    def test_timeouts_are_bounded_under_the_gateway_budget(self, monkeypatch):
        monkeypatch.setenv("S3_ENDPOINT_URL", VPCE_ENDPOINT)
        cfg = _reload().s3_client.meta.config
        assert cfg.connect_timeout <= 5
        assert cfg.read_timeout <= 10
        worst_case = _worst_case_seconds(cfg)
        assert worst_case < 29, (
            f"worst-case S3 wait is {worst_case}s, which exceeds the 29s REST API "
            "Gateway integration ceiling — the caller would see a bodiless 504"
        )
