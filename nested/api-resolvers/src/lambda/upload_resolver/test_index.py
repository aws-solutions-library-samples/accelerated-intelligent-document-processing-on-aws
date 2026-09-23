# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the sample-document operations in upload_resolver.

Covers listSampleDocuments (manifest read) and uploadSampleDocument
(server-side copy of a bundled sample from the ConfigurationBucket into the
InputBucket, including config-version metadata and batch expansion).
"""

import importlib
import json

import boto3
import pytest
from moto import mock_aws

CONFIG_BUCKET = "config-bucket"
INPUT_BUCKET = "input-bucket"

MANIFEST = {
    "schemaVersion": "1.0",
    "samples": [
        {
            "id": "bank-statement-multipage",
            "name": "Bank Statement (multi-page)",
            "description": "desc",
            "s3Key": "samples/bank-statement-multipage.pdf",
            "kind": "document",
            "fileCount": 1,
            "configId": "bank-statement-sample",
        },
        {
            "id": "w2",
            "name": "W-2 Forms",
            "description": "desc",
            "s3Key": "samples/w2/",
            "kind": "batch",
            "fileCount": 2,
            "configId": "fake-w2",
        },
    ],
}


def _event(field, arguments=None, groups=("Admin",)):
    return {
        "info": {"fieldName": field},
        "arguments": arguments or {},
        "identity": {"claims": {"cognito:groups": list(groups)}},
    }


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_BUCKET", CONFIG_BUCKET)
    monkeypatch.setenv("INPUT_BUCKET", INPUT_BUCKET)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=CONFIG_BUCKET)
        s3.create_bucket(Bucket=INPUT_BUCKET)
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key="config_library/samples-manifest.json",
            Body=json.dumps(MANIFEST).encode(),
        )
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key="samples/bank-statement-multipage.pdf",
            Body=b"%PDF-1.4 statement",
        )
        s3.put_object(Bucket=CONFIG_BUCKET, Key="samples/w2/W2_0.pdf", Body=b"%PDF a")
        s3.put_object(Bucket=CONFIG_BUCKET, Key="samples/w2/W2_1.pdf", Body=b"%PDF b")

        # Import after the mock + env are in place so the module-level S3 client
        # is created against moto.
        import index

        importlib.reload(index)
        yield index, s3


@pytest.mark.unit
def test_list_sample_documents(resolver):
    index, _ = resolver
    result = index.handler(_event("listSampleDocuments", groups=("Viewer",)))
    assert result["success"] is True
    ids = {s["id"] for s in result["samples"]}
    assert ids == {"bank-statement-multipage", "w2"}


@pytest.mark.unit
def test_list_sample_documents_denies_unauthorized(resolver):
    index, _ = resolver
    with pytest.raises(PermissionError):
        index.handler(_event("listSampleDocuments", groups=("Reviewer",)))


@pytest.mark.unit
def test_upload_sample_document_copies_with_version_metadata(resolver):
    index, s3 = resolver
    result = index.handler(
        _event(
            "uploadSampleDocument",
            {"sampleId": "bank-statement-multipage", "prefix": "demo", "version": "bank-statement-sample"},
        )
    )
    assert result["success"] is True
    assert result["objectKeys"] == ["demo/bank-statement-multipage.pdf"]

    head = s3.head_object(Bucket=INPUT_BUCKET, Key="demo/bank-statement-multipage.pdf")
    assert head["Metadata"]["config-version"] == "bank-statement-sample"


@pytest.mark.unit
def test_upload_sample_document_batch_expands_all_files(resolver):
    index, s3 = resolver
    result = index.handler(
        _event("uploadSampleDocument", {"sampleId": "w2", "version": "fake-w2"})
    )
    assert result["success"] is True
    assert sorted(result["objectKeys"]) == ["W2_0.pdf", "W2_1.pdf"]
    listing = s3.list_objects_v2(Bucket=INPUT_BUCKET)
    assert {o["Key"] for o in listing["Contents"]} == {"W2_0.pdf", "W2_1.pdf"}


@pytest.mark.unit
def test_upload_sample_document_unknown_id(resolver):
    index, _ = resolver
    result = index.handler(_event("uploadSampleDocument", {"sampleId": "nope"}))
    assert result["success"] is False
    assert "Unknown sampleId" in result["error"]


@pytest.mark.unit
def test_upload_sample_document_denies_viewer(resolver):
    index, _ = resolver
    with pytest.raises(PermissionError):
        index.handler(
            _event("uploadSampleDocument", {"sampleId": "w2"}, groups=("Viewer",))
        )


# ---------------------------------------------------------------------------
# Which bucket and key an upload may target
#
# `bucket` and `prefix` are request arguments, and this function's role holds write
# on every bucket the deployment uses, so the request is bounded here or not at all.
# Two controls, from the shared `s3_targets` rule: the same bucket allow-list the read
# path in get_file_contents_resolver applies, plus a write-once key rule that write
# paths consult and the read path does not.
#
# `test_a_legitimate_upload_still_succeeds` is the one that has to hold: this is a
# live path the UI uses on every document upload, so a control that over-refuses
# breaks uploading rather than protecting it.
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestTheUploadTargetIsConstrained:
    def test_a_legitimate_upload_still_succeeds(self, resolver):
        index, _ = resolver

        result = index.handler(
            _event(
                "uploadDocument",
                {"fileName": "invoice.pdf", "prefix": "lending"},
            )
        )

        assert result["objectKey"] == "lending/invoice.pdf"
        assert result["presignedUrl"]

    def test_an_upload_with_no_prefix_still_succeeds(self, resolver):
        index, _ = resolver

        result = index.handler(_event("uploadDocument", {"fileName": "scan.pdf"}))

        assert result["objectKey"] == "scan.pdf"

    def test_a_bucket_outside_the_deployment_is_refused(self, resolver):
        index, _ = resolver

        with pytest.raises(PermissionError) as excinfo:
            index.handler(
                _event(
                    "uploadDocument",
                    {"fileName": "x.pdf", "bucket": "someone-elses-bucket"},
                )
            )

        assert str(excinfo.value).startswith("Unauthorized")
        assert "someone-elses-bucket" not in str(excinfo.value)

    def test_a_write_once_prefix_is_refused(self, resolver):
        """Some keys hold objects whose integrity comes from the key: written once,
        then read as the record of what happened."""
        index, _ = resolver

        with pytest.raises(PermissionError) as excinfo:
            index.handler(
                _event(
                    "uploadDocument",
                    {
                        "fileName": "000001.json.gz",
                        "prefix": "config_revisions/default",
                        "bucket": CONFIG_BUCKET,
                    },
                )
            )

        assert str(excinfo.value).startswith("Unauthorized")

    def test_the_other_write_once_store_is_refused_too(self, resolver):
        index, _ = resolver

        with pytest.raises(PermissionError):
            index.handler(
                _event(
                    "uploadDocument",
                    {
                        "fileName": "manifest.json",
                        "prefix": "mydoc.pdf/runs/20260101T000000Z-abc",
                    },
                )
            )

    def test_no_presigned_url_is_minted_for_a_refused_target(self, resolver):
        """The refusal has to precede the mint, or the capability already exists."""
        index, s3 = resolver
        minted = []
        original = index.s3_client.generate_presigned_post

        def _spy(**kwargs):
            minted.append(kwargs)
            return original(**kwargs)

        index.s3_client.generate_presigned_post = _spy
        try:
            with pytest.raises(PermissionError):
                index.handler(
                    _event(
                        "uploadDocument",
                        {"fileName": "x.pdf", "bucket": "someone-elses-bucket"},
                    )
                )
        finally:
            index.s3_client.generate_presigned_post = original

        assert minted == [], "a presigned URL was minted for a refused target"

    def test_a_sample_copy_into_a_write_once_prefix_is_refused(self, resolver):
        """The sample-copy path takes `prefix` from the request too.

        Uses the revision prefix rather than a run path: the run rule is scoped to the
        manifest object itself, and the copied file's name comes from the sample
        manifest, so a run path here would not land on the protected key. That the
        rule declines to refuse it is correct — see the note on WRITE_ONCE_KEY_RULES
        about why it is not widened to the whole `runs/` directory.
        """
        index, _ = resolver

        with pytest.raises(PermissionError):
            index.handler(
                _event(
                    "uploadSampleDocument",
                    {
                        "sampleId": "bank-statement-multipage",
                        "prefix": "config_revisions/default",
                    },
                )
            )

    def test_a_document_merely_under_a_runs_path_is_not_refused(self, resolver):
        """The run rule protects the manifest, not every key beneath `runs/`.

        A document key can legitimately contain `runs` as a path segment, and
        refusing those would break uploads for the sake of a key nothing writes.
        """
        index, _ = resolver

        result = index.handler(
            _event(
                "uploadDocument",
                {"fileName": "report.pdf", "prefix": "2026/runs/january"},
            )
        )

        assert result["objectKey"] == "2026/runs/january/report.pdf"

    def test_a_sample_copy_to_an_ordinary_prefix_still_succeeds(self, resolver):
        index, _ = resolver

        result = index.handler(
            _event(
                "uploadSampleDocument",
                {"sampleId": "bank-statement-multipage", "prefix": "inbox"},
            )
        )

        assert result["success"] is True
        assert result["objectKeys"] == ["inbox/bank-statement-multipage.pdf"]


@pytest.mark.unit
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
        with mock_aws():
            import index

            importlib.reload(index)
            assert index.ALLOWED_BUCKETS == set()

            with pytest.raises(PermissionError, match="not configured"):
                index.handler(
                    _event("uploadDocument", {"fileName": "x.pdf", "bucket": "b"})
                )
