# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for get_file_contents_resolver.

Covers both resolver fields:
  - getFileContents      -> inline file bytes (6 MB Lambda cap)
  - getFilePresignedUrl  -> presigned GET URL (no size limit; browser fetches
                            directly from S3)
"""

import importlib

import boto3
import pytest
from moto import mock_aws

OUTPUT_BUCKET = "output-bucket"
OTHER_BUCKET = "some-unrelated-bucket"


def _event(field, s3_uri, version_id=None):
    args = {"s3Uri": s3_uri}
    if version_id is not None:
        args["versionId"] = version_id
    return {
        "info": {"fieldName": field},
        "arguments": args,
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
    }


def _response_params(url):
    """The response-* overrides on a presigned URL, parsed (encoding varies)."""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    return {k.lower(): v for k, v in query.items() if k.lower().startswith("response-")}


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setenv("OUTPUT_BUCKET", OUTPUT_BUCKET)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=OUTPUT_BUCKET)
        s3.create_bucket(Bucket=OTHER_BUCKET)
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key="doc/sections/1/result.json",
            Body=b'{"hello": "world"}',
            ContentType="application/json",
        )

        # Import after mock + env are in place so the module-level S3 client and
        # ALLOWED_BUCKETS are built against moto/the test env.
        import index

        importlib.reload(index)
        yield index, s3


@pytest.mark.unit
def test_get_file_contents_returns_inline_bytes(resolver):
    index, _ = resolver
    result = index.handler(
        _event("getFileContents", f"s3://{OUTPUT_BUCKET}/doc/sections/1/result.json"),
        None,
    )
    assert result["content"] == '{"hello": "world"}'
    assert result["contentType"] == "application/json"
    assert result["isBinary"] is False


@pytest.mark.unit
def test_get_file_presigned_url_returns_url_and_metadata(resolver):
    index, _ = resolver
    result = index.handler(
        _event(
            "getFilePresignedUrl",
            f"s3://{OUTPUT_BUCKET}/doc/sections/1/result.json",
        ),
        None,
    )
    assert result["presignedUrl"].startswith("https://")
    assert "doc/sections/1/result.json" in result["presignedUrl"]
    assert result["contentType"] == "application/json"
    assert result["size"] == len(b'{"hello": "world"}')
    # Must NOT return the file bytes inline.
    assert "content" not in result


@pytest.mark.unit
def test_get_file_presigned_url_missing_object_raises(resolver):
    index, _ = resolver
    with pytest.raises(Exception, match="File not found"):
        index.handler(
            _event("getFilePresignedUrl", f"s3://{OUTPUT_BUCKET}/doc/does-not-exist.json"),
            None,
        )


@pytest.mark.unit
def test_bucket_allow_list_enforced_for_presigned_url(resolver):
    index, _ = resolver
    with pytest.raises(Exception, match="Error fetching file|Unauthorized"):
        index.handler(
            _event("getFilePresignedUrl", f"s3://{OTHER_BUCKET}/secret.json"),
            None,
        )


@pytest.mark.unit
def test_invalid_uri_raises(resolver):
    index, _ = resolver
    with pytest.raises(Exception):
        index.handler(_event("getFilePresignedUrl", "not-an-s3-uri"), None)


@pytest.mark.unit
def test_uri_missing_key_raises(resolver):
    index, _ = resolver
    with pytest.raises(Exception, match="Invalid S3 URI"):
        index.handler(_event("getFilePresignedUrl", f"s3://{OUTPUT_BUCKET}"), None)


# Object keys may legitimately contain '#' (document ids like
# "Report_#2.pdf"). urlparse-based parsing truncated the key at '#'
# (fragment delimiter), yielding NoSuchKey; these tests pin the fix.
HASH_KEY = "Report_#2.pdf/pages/1/result.json"


@pytest.mark.unit
def test_get_file_contents_key_with_hash(resolver):
    index, s3 = resolver
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=HASH_KEY,
        Body=b'{"page": 1}',
        ContentType="application/json",
    )
    result = index.handler(
        _event("getFileContents", f"s3://{OUTPUT_BUCKET}/{HASH_KEY}"),
        None,
    )
    assert result["content"] == '{"page": 1}'
    assert result["isBinary"] is False


@pytest.mark.unit
def test_get_file_presigned_url_key_with_hash(resolver):
    index, s3 = resolver
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=HASH_KEY,
        Body=b'{"page": 1}',
        ContentType="application/json",
    )
    result = index.handler(
        _event("getFilePresignedUrl", f"s3://{OUTPUT_BUCKET}/{HASH_KEY}"),
        None,
    )
    assert result["presignedUrl"].startswith("https://")
    assert result["size"] == len(b'{"page": 1}')


@pytest.mark.unit
def test_presigned_url_forces_pdf_content_type_and_inline(resolver):
    """Regression: a PDF stored as octet-stream must still render in-page.

    Reported live: "View source document" downloaded the file instead of showing
    it. The synthetic generator uploaded PDFs without a ContentType, leaving them
    as binary/octet-stream in S3; the presigned URL passed that through, and a
    browser handed octet-stream downloads rather than rendering. A zip-uploaded
    test set worked, which made it look like a viewer bug.

    The URL now overrides the response headers, which also repairs objects already
    stored with the wrong type.
    """
    index, s3 = resolver
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key="doc/pages/1/page.pdf",
        Body=b"%PDF-1.4 fake",
        ContentType="binary/octet-stream",
    )

    result = index.handler(
        _event("getFilePresignedUrl", f"s3://{OUTPUT_BUCKET}/doc/pages/1/page.pdf"),
        None,
    )

    assert result["contentType"] == "application/pdf"
    params = _response_params(result["presignedUrl"])
    assert params.get("response-content-type") == ["application/pdf"]
    assert params.get("response-content-disposition") == ["inline"]


@pytest.mark.unit
def test_presigned_url_keeps_images_and_text_inline(resolver):
    """Images and text are browser-renderable, so they display rather than download."""
    index, s3 = resolver
    for key, stored, expected in (
        ("doc/pages/1/page.jpg", "binary/octet-stream", "image/jpeg"),
        ("doc/pages/1/page.txt", "binary/octet-stream", "text/plain"),
    ):
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=b"x", ContentType=stored)
        result = index.handler(
            _event("getFilePresignedUrl", f"s3://{OUTPUT_BUCKET}/{key}"), None
        )
        assert result["contentType"] == expected, key
        params = _response_params(result["presignedUrl"])
        assert params.get("response-content-disposition") == ["inline"], key


@pytest.mark.unit
def test_presigned_url_does_not_force_inline_for_non_renderable_types(resolver):
    """A spreadsheet must keep downloading — inline would render as gibberish."""
    index, s3 = resolver
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key="doc/report.xlsx",
        Body=b"PK fake xlsx",
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    result = index.handler(
        _event("getFilePresignedUrl", f"s3://{OUTPUT_BUCKET}/doc/report.xlsx"), None
    )

    assert "response-content-disposition" not in _response_params(result["presignedUrl"])


@pytest.mark.unit
def test_is_inline_renderable_classification(resolver):
    index, _ = resolver
    assert index._is_inline_renderable("application/pdf")
    assert index._is_inline_renderable("APPLICATION/PDF")
    assert index._is_inline_renderable("text/plain; charset=utf-8")
    assert index._is_inline_renderable("image/png")
    assert not index._is_inline_renderable("application/octet-stream")
    assert not index._is_inline_renderable("")
    assert not index._is_inline_renderable(None)


@pytest.mark.unit
def test_script_bearing_types_are_forced_to_download(resolver):
    """An uploaded .html/.svg must not render on the bucket origin.

    Both fall under renderable prefixes (text/, image/), so the earlier rule served
    them inline. Declining to say "inline" is not enough either: with no disposition
    the browser decides from Content-Type alone and still renders text/html.
    """
    index, s3 = resolver
    for key, stored in (
        ("doc/evil.html", "text/html"),
        ("doc/evil.svg", "image/svg+xml"),
        # Uploaded without a type, so the resolver guesses from the extension.
        ("doc/guessed.html", "binary/octet-stream"),
    ):
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=key,
            Body=b"<script>1</script>",
            ContentType=stored,
        )
        result = index.handler(
            _event("getFilePresignedUrl", f"s3://{OUTPUT_BUCKET}/{key}"), None
        )
        params = _response_params(result["presignedUrl"])
        assert params.get("response-content-disposition") == ["attachment"], key


@pytest.mark.unit
def test_executable_type_classification(resolver):
    index, _ = resolver
    assert not index._is_inline_renderable("text/html")
    assert not index._is_inline_renderable("image/svg+xml")
    assert not index._is_inline_renderable("TEXT/HTML; charset=utf-8")
    assert index._is_executable_type("text/html")
    # Plain text and raster images stay renderable.
    assert index._is_inline_renderable("text/plain")
    assert index._is_inline_renderable("image/png")
    assert not index._is_executable_type("image/png")
