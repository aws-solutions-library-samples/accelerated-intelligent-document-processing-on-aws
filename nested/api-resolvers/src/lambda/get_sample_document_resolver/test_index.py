# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for get_sample_document_resolver.

Covers the RBAC gate (Admin/Author/Viewer, parity with listSampleDocuments),
the samples/ key validation, and the presigned-URL happy path.
"""

import importlib

import boto3
import pytest
from moto import mock_aws

CONFIG_BUCKET = "config-bucket"


def _event(s3_key, groups=("Viewer",)):
    return {
        "arguments": {"s3Key": s3_key},
        "identity": {"claims": {"cognito:groups": list(groups)}},
    }


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setenv("CONFIGURATION_BUCKET", CONFIG_BUCKET)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=CONFIG_BUCKET)
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key="samples/bank-statement.pdf",
            Body=b"%PDF-1.4 statement",
        )

        import index

        importlib.reload(index)
        yield index


@pytest.mark.unit
def test_presigns_for_authorized_caller(resolver):
    result = resolver.handler(_event("samples/bank-statement.pdf", groups=("Viewer",)))
    assert result["s3Key"] == "samples/bank-statement.pdf"
    assert result["url"].startswith("https://")
    # Inline disposition + a real content-type are forced on the URL so the
    # browser opens (rather than downloads) the octet-stream-uploaded object.
    assert "response-content-disposition=inline" in result["url"]


@pytest.mark.unit
@pytest.mark.parametrize("groups", [("Admin",), ("Author",), ("Viewer",)])
def test_allows_each_authorized_group(resolver, groups):
    result = resolver.handler(_event("samples/bank-statement.pdf", groups=groups))
    assert result["url"].startswith("https://")


@pytest.mark.unit
def test_denies_unauthorized_group(resolver):
    with pytest.raises(PermissionError):
        resolver.handler(_event("samples/bank-statement.pdf", groups=("Reviewer",)))


@pytest.mark.unit
def test_denies_missing_groups(resolver):
    event = {"arguments": {"s3Key": "samples/bank-statement.pdf"}, "identity": {}}
    with pytest.raises(PermissionError):
        resolver.handler(event)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_key",
    ["", "/etc/passwd", "config_library/secret.yaml", "samples/../config/x", "other/x"],
)
def test_rejects_non_samples_keys(resolver, bad_key):
    with pytest.raises(ValueError):
        resolver.handler(_event(bad_key, groups=("Admin",)))


@pytest.mark.unit
def test_zip_served_as_attachment(resolver):
    # A batch sample zip should download rather than open inline.
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.put_object(Bucket=CONFIG_BUCKET, Key="samples/w2.zip", Body=b"PK\x03\x04")
    result = resolver.handler(_event("samples/w2.zip", groups=("Admin",)))
    assert "response-content-disposition=attachment" in result["url"]


@pytest.mark.unit
def test_presigns_nested_batch_file(resolver):
    # A representative file inside a batch folder (samples/<batch>/<file>) is
    # openable inline — this is how the agent links one example from a batch.
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.put_object(Bucket=CONFIG_BUCKET, Key="samples/w2/W2_0.pdf", Body=b"%PDF w2")
    result = resolver.handler(_event("samples/w2/W2_0.pdf", groups=("Viewer",)))
    assert result["s3Key"] == "samples/w2/W2_0.pdf"
    assert "response-content-disposition=inline" in result["url"]


@pytest.mark.unit
def test_a_presign_failure_is_reported_with_a_message_for_the_caller(
    resolver, monkeypatch
):
    """``ReportedServerError`` is the class the dispatcher passes a 500 message
    through for; any other class now reaches the caller as a generic message."""
    from botocore.exceptions import ClientError

    def _fail(*_args, **_kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "arn:aws:iam::1:role/x"}},
            "GeneratePresignedUrl",
        )

    monkeypatch.setattr(resolver.s3_client, "generate_presigned_url", _fail)
    with pytest.raises(Exception) as excinfo:
        resolver.handler(_event("samples/bank-statement.pdf"))
    assert type(excinfo.value).__name__ == "ReportedServerError"
    assert str(excinfo.value) == "Could not generate a link for the requested sample"
