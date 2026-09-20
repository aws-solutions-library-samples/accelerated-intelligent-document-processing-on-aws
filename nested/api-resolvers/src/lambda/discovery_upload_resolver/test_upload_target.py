# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which S3 bucket and key the discovery upload path may target.

`bucket` and `prefix` are request arguments and this function's role holds write on the
discovery bucket, so the request is bounded here or not at all. The check sits in
`create_s3_signed_post_url`, the one choke point every presign path in the module goes
through, rather than at each caller — a per-caller check is one a new caller can be
added without.

The rule is the shared `s3_targets` one, the same allow-list the read path in
`get_file_contents_resolver` applies; this function carries the `idp_common` layer so
it imports it rather than vendoring a copy.

`test_a_legitimate_upload_still_succeeds` is the control that matters: this is a live
path, so a constraint that over-refuses breaks discovery uploads.
"""

import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DISCOVERY_BUCKET = "discovery-bucket"

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("DISCOVERY_BUCKET", DISCOVERY_BUCKET)
    monkeypatch.setenv("DISCOVERY_TABLE", "IDP-DiscoveryTable")
    import index as module

    importlib.reload(module)
    return module


class TestTheUploadTargetIsConstrained:
    def test_a_legitimate_upload_still_succeeds(self, index):
        key, presigned = index.create_s3_signed_post_url(
            DISCOVERY_BUCKET, "application/pdf", "scan.pdf", "document", ""
        )

        assert key.startswith("document/")
        assert key.endswith("_scan.pdf")
        assert presigned

    def test_an_upload_under_a_caller_prefix_still_succeeds(self, index):
        key, _ = index.create_s3_signed_post_url(
            DISCOVERY_BUCKET, "application/pdf", "scan.pdf", "document", "batch-1"
        )

        assert key.startswith("batch-1/document/")

    def test_a_bucket_outside_the_deployment_is_refused(self, index):
        with pytest.raises(PermissionError) as excinfo:
            index.create_s3_signed_post_url(
                "someone-elses-bucket", "application/pdf", "x.pdf", "document", ""
            )

        assert str(excinfo.value).startswith("Unauthorized")
        assert "someone-elses-bucket" not in str(excinfo.value)

    def test_a_write_once_prefix_is_refused(self, index):
        with pytest.raises(PermissionError) as excinfo:
            index.create_s3_signed_post_url(
                DISCOVERY_BUCKET,
                "application/gzip",
                "000001.json.gz",
                "x",
                "config_revisions/default",
            )

        assert str(excinfo.value).startswith("Unauthorized")

    def test_no_presigned_url_is_minted_for_a_refused_target(self, index,
                                                             monkeypatch):
        """The refusal must precede the mint, or the capability already exists."""
        minted = []
        monkeypatch.setattr(
            index.s3_client,
            "generate_presigned_post",
            lambda **kw: minted.append(kw) or {},
        )

        with pytest.raises(PermissionError):
            index.create_s3_signed_post_url(
                "someone-elses-bucket", "application/pdf", "x.pdf", "document", ""
            )

        assert minted == []

    def test_the_read_path_refuses_a_foreign_bucket_too(self, index):
        """`autoDetectSections` reads a caller-named bucket; the allow-list applies,
        the write-once key rule does not."""
        with pytest.raises(PermissionError):
            index.handle_auto_detect_sections(
                {
                    "arguments": {
                        "documentKey": "x.pdf",
                        "bucket": "someone-elses-bucket",
                    }
                },
                None,
            )


class TestTheAllowListFailsClosed:
    def test_an_unconfigured_allow_list_refuses_every_upload(self, monkeypatch):
        for name in (
            "INPUT_BUCKET",
            "OUTPUT_BUCKET",
            "CONFIGURATION_BUCKET",
            "EVALUATION_BASELINE_BUCKET",
            "REPORTING_BUCKET",
            "TEST_SET_BUCKET",
            "DISCOVERY_BUCKET",
            "WORKING_BUCKET",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        import index as module

        importlib.reload(module)
        assert module.ALLOWED_BUCKETS == set()

        with pytest.raises(PermissionError, match="not configured"):
            module.create_s3_signed_post_url(
                "any-bucket", "application/pdf", "x.pdf", "document", ""
            )
