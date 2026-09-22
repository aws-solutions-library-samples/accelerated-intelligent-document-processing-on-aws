#!/usr/bin/env python3
"""
Test script for S3 Vectors custom resource handler.
This script validates the API calls and logic without requiring CloudFormation.

Every test here was checked by mutating ``handler.py`` and confirming that it, and
only it, went red. If you re-run that check, **delete ``__pycache__`` between
mutations** — one of the useful mutations here is the same LENGTH as the code it
replaces (swapping the arms of the status ternary), and CPython keys ``.pyc``
invalidation on ``(mtime, size)``, so it can leave stale bytecode considered valid.
``-B`` alone does not help: it stops a ``.pyc`` being written, not one being read.
See "Proving a test is load-bearing" in ``.claude/skills/testing-qa.md``.
"""

import logging
import os
import re
import sys
from unittest.mock import Mock, patch

import botocore.session
from botocore.exceptions import ClientError

# Import the handler functions
from handler import (
    create_s3_vector_resources,
    create_vector_index,
    get_s3_vector_info,
    is_valid_s3_bucket_name,
    sanitize_bucket_name,
)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_sanitize_bucket_name():
    """Each documented sanitization rule, pinned to the name it actually produces.

    The name this returns is the bucket the handler creates, and the IAM policy in
    the parent template is written against its *shape* (see
    ``tests/test_iam_scope.py``), so a change in what any of these inputs maps to
    is a deploy-time failure rather than a cosmetic one. Every case below names the
    rule it exercises; the final assertion is the invariant the whole function
    exists for, and is the one ``create_s3_vector_resources`` raises ``ValueError``
    on if it is ever violated.
    """
    cases = [
        # (input, expected, the rule it exercises)
        ("TestBucket", "testbucket", "uppercase is lowered"),
        ("Test_Bucket_123", "test-bucket-123", "invalid characters become hyphens"),
        ("TEST-BUCKET-NAME", "test-bucket-name", "existing hyphens are preserved"),
        ("", "default-s3-vectors", "an empty name falls back to a default"),
        ("a", "s3vectors-a", "a name under 3 characters is prefixed"),
        ("Test--Bucket", "test-bucket", "consecutive hyphens collapse"),
        # `strip('-')` runs before the leading/trailing-hyphen guards below it, so
        # those guards never fire for this input and the result keeps no affix.
        ("-test-bucket-", "test-bucket", "leading and trailing hyphens are stripped"),
        # 70 characters in, 63 out: truncated to 60 plus the `-kb` suffix.
        ("x" * 70, "x" * 60 + "-kb", "a name over 63 characters is truncated"),
    ]

    for raw, expected, rule in cases:
        result = sanitize_bucket_name(raw)
        assert result == expected, (
            f"sanitize_bucket_name({raw!r}) == {result!r}, expected {expected!r} "
            f"({rule})"
        )
        assert is_valid_s3_bucket_name(result), (
            f"sanitize_bucket_name({raw!r}) returned {result!r}, which "
            "is_valid_s3_bucket_name rejects — create_s3_vector_resources raises "
            "ValueError on such a name, failing the stack deployment"
        )


def test_s3_vectors_api_methods():
    """Every s3vectors operation the handler calls must exist in the real API.

    This is the one check here that a mock cannot perform. Every other test in this
    file drives ``handler.py`` against a ``Mock``, and a ``Mock`` answers to any
    attribute name at all — so a misspelled or renamed operation is invisible at
    that level and surfaces only as a stack rollback. ``botocore`` ships the
    service model, which is a local, offline source of truth for what ``s3vectors``
    actually offers.

    The handler was written against an API surface its author was unsure of — the
    comment at ``get_s3_vector_info`` calls existence checks "potentially
    non-existent API methods" — which is exactly the situation this guards.

    The call list is scanned out of the source rather than restated, so a new call
    is covered without editing this test. ``tests/test_iam_scope.py`` scans the same
    way for a different purpose (that the IAM policy grants exactly these actions);
    neither subsumes the other, since a rename applied consistently to the handler
    *and* the template satisfies that one and fails here.
    """
    handler_source = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "handler.py"
    )
    with open(handler_source, encoding="utf-8") as source_file:
        called = set(re.findall(r"s3vectors_client\.([a-z_]+)\(", source_file.read()))
    assert called, (
        "no `s3vectors_client.<method>(` calls found in handler.py — the scan is "
        "broken, so this test would pass no matter what the handler called"
    )

    model = botocore.session.get_session().get_service_model("s3vectors")
    # botocore names operations in PascalCase; boto3 exposes them snake_cased.
    # Comparing on a case- and underscore-insensitive key avoids reimplementing
    # that conversion in either direction.
    available = {name.lower() for name in model.operation_names}
    unknown = sorted(
        method for method in called if method.replace("_", "") not in available
    )
    assert not unknown, (
        f"handler.py calls s3vectors operations that the installed botocore's "
        f"service model does not define: {unknown}. Either the name is misspelled "
        f"or the API changed; the deployment would fail at runtime. Known "
        f"operations: {sorted(model.operation_names)}"
    )


def test_create_vector_index_function():
    """Test the create_vector_index function with mocked client."""
    print("Testing create_vector_index function...")

    # Mock S3 Vectors client
    mock_client = Mock()
    mock_client.meta.region_name = "us-west-2"

    # Test successful index creation
    mock_client.create_index.return_value = {"IndexName": "test-index"}

    result = create_vector_index(mock_client, "test-bucket", "test-index")

    # Verify the create_index was called with correct parameters
    mock_client.create_index.assert_called_once_with(
        vectorBucketName="test-bucket",
        indexName="test-index",
        dataType="float32",
        dimension=1024,
        distanceMetric="cosine",
        metadataConfiguration={
            "nonFilterableMetadataKeys": [
                "AMAZON_BEDROCK_METADATA",
                "AMAZON_BEDROCK_TEXT",
            ]
        },
    )

    print("  ✓ create_vector_index called with correct parameters")

    # Test conflict exception handling
    mock_client.reset_mock()
    mock_client.create_index.side_effect = ClientError(
        {"Error": {"Code": "ConflictException"}}, "create_index"
    )

    result = create_vector_index(mock_client, "test-bucket", "test-index")
    assert result is None, "Should return None for ConflictException"
    print("  ✓ ConflictException handled correctly")

    print("✓ create_vector_index function tests completed")


def test_get_s3_vector_info_function():
    """Both arms of the Status the handler reports for an existing bucket.

    ``get_s3_vector_info`` deliberately does not ask whether the index exists — the
    comment in it explains why — so the *only* thing that distinguishes its two
    outcomes is how ``create_index`` responds: ``ConflictException`` means the index
    was already there (``Existing``), and a successful response means this call made
    it (``IndexCreated``). Driving the branch through ``create_index`` is therefore
    the only way to reach either arm, and both are asserted here because nothing
    else pins that ternary — with one arm unasserted it could be inverted
    undetected.

    The index ARN is compared in full rather than merely for presence: it is built
    by string interpolation and handed to the Bedrock Knowledge Base as the vector
    store, so a wrong partition, region or path segment is a silent
    misconfiguration rather than an error.
    """
    bucket_arn = "arn:aws:s3vectors:us-west-2:123456789012:bucket/test-bucket"
    expected_index_arn = f"{bucket_arn}/index/test-index"

    # Mock S3 Vectors client
    mock_client = Mock()
    mock_client.meta.region_name = "us-west-2"

    # Mock STS client for account ID
    with patch("boto3.client") as mock_boto3:
        mock_sts = Mock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}

        def client_factory(service, **kwargs):
            if service == "sts":
                return mock_sts
            return mock_client

        mock_boto3.side_effect = client_factory

        # Case 1: the index is already there, so create_index conflicts.
        mock_client.get_vector_bucket.return_value = {"BucketArn": bucket_arn}
        mock_client.create_index.side_effect = ClientError(
            {"Error": {"Code": "ConflictException"}},
            "create_index",
        )

        result = get_s3_vector_info(mock_client, "test-bucket", "test-index")

        assert result["BucketName"] == "test-bucket"
        assert result["IndexName"] == "test-index"
        assert result["BucketArn"] == bucket_arn
        assert result["IndexArn"] == expected_index_arn
        assert result["Status"] == "Existing", (
            "a ConflictException from create_index means the index already "
            "existed, which the handler reports as 'Existing'"
        )
        mock_client.create_index.assert_called_once()

        # Case 2: the index is absent, so create_index succeeds and this call
        # is the one that created it.
        mock_client.reset_mock()
        mock_client.create_index.side_effect = None
        mock_client.get_vector_bucket.return_value = {"BucketArn": bucket_arn}
        mock_client.create_index.return_value = {"IndexName": "test-index"}

        result = get_s3_vector_info(mock_client, "test-bucket", "test-index")

        assert result["Status"] == "IndexCreated"
        assert result["IndexArn"] == expected_index_arn
        mock_client.create_index.assert_called_once()


def test_full_workflow_simulation():
    """Simulate a full CloudFormation CREATE workflow."""
    print("Testing full workflow simulation...")

    # Mock all external dependencies
    with patch("boto3.client") as mock_boto3:
        mock_s3v_client = Mock()
        mock_s3v_client.meta.region_name = "us-west-2"
        mock_sts_client = Mock()
        mock_sts_client.get_caller_identity.return_value = {"Account": "123456789012"}

        def client_factory(service, **kwargs):
            if service == "sts":
                return mock_sts_client
            elif service == "s3vectors":
                return mock_s3v_client
            return Mock()

        mock_boto3.side_effect = client_factory

        # Simulate successful bucket and index creation
        mock_s3v_client.create_vector_bucket.return_value = {
            "BucketName": "test-bucket"
        }
        mock_s3v_client.create_index.return_value = {"IndexName": "test-index"}

        result = create_s3_vector_resources(
            mock_s3v_client, "test-bucket", "test-index", "amazon.titan-embed-text-v2:0"
        )

        assert result["BucketName"] == "test-bucket"
        assert result["IndexName"] == "test-index"
        assert "IndexArn" in result
        assert result["Status"] == "Created"

        print("  ✓ Full CREATE workflow completed successfully")

    print("✓ Full workflow simulation tests completed")


def run_all_tests():
    """Run all test functions."""
    print("=" * 60)
    print("Running S3 Vectors Handler Tests")
    print("=" * 60)

    try:
        test_sanitize_bucket_name()
        print()

        test_s3_vectors_api_methods()
        print()

        test_create_vector_index_function()
        print()

        test_get_s3_vector_info_function()
        print()

        test_full_workflow_simulation()
        print()

        print("=" * 60)
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        return True

    except Exception as e:
        print(f"✗ TEST FAILED: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
