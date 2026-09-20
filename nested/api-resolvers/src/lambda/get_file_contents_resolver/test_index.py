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
    with pytest.raises(ValueError, match="File not found"):
        index.handler(
            _event("getFilePresignedUrl", f"s3://{OUTPUT_BUCKET}/doc/does-not-exist.json"),
            None,
        )


@pytest.mark.unit
def test_bucket_allow_list_enforced_for_presigned_url(resolver):
    index, _ = resolver
    # `PermissionError`, not a bare Exception matching "Error fetching
    # field|Unauthorized". The previous alternation passed whether or not the
    # refusal survived as an authorization signal, which is how it went unnoticed
    # that the handler's catch-all rewrapped it into a 500 `InternalError`.
    with pytest.raises(PermissionError, match="^Unauthorized:"):
        index.handler(
            _event("getFilePresignedUrl", f"s3://{OTHER_BUCKET}/secret.json"),
            None,
        )


@pytest.mark.unit
def test_invalid_uri_raises(resolver):
    index, _ = resolver
    with pytest.raises(ValueError, match="Invalid S3 URI"):
        index.handler(_event("getFilePresignedUrl", "not-an-s3-uri"), None)


@pytest.mark.unit
def test_uri_missing_key_raises(resolver):
    index, _ = resolver
    with pytest.raises(ValueError, match="Invalid S3 URI"):
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


# ---------------------------------------------------------------------------
# What status the caller actually receives
#
# Every refusal in this file used to arrive as HTTP 500 `InternalError`, because
# the handler's catch-all rewrapped each one as `Exception(f"Error fetching file:
# {e}")` — destroying the exception class name AND pushing the "Unauthorized"
# token off the front of the message, which are the only two things
# `http_api_dispatcher` uses to choose a status. The consequences were an operator
# chasing a phantom server fault for a legitimate 403, and a 5xx rate that counted
# deliberate policy denials, so a real spike in faults was masked by them and an
# attacker probing bucket names inflated the fault signal rather than the
# denial one.
#
# These tests assert the class the dispatcher keys on rather than the status,
# because the mapping itself is pinned in
# lib/idp_common_pkg/tests/unit/test_http_api_dispatcher_*.py.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRefusalsAreDistinguishableFromFaults:
    @pytest.mark.parametrize("field", ["getFileContents", "getFilePresignedUrl"])
    def test_the_bucket_refusal_is_an_authorization_refusal(self, resolver, field):
        """Both fields share this resolver, so both must refuse the same way."""
        index, _ = resolver

        with pytest.raises(PermissionError) as excinfo:
            index.handler(_event(field, f"s3://{OTHER_BUCKET}/secret.json"), None)

        # The dispatcher matches on the class name, and falls back to a message
        # starting "Unauthorized"/"Forbidden". Both must hold.
        assert type(excinfo.value).__name__ == "PermissionError"
        assert str(excinfo.value).startswith("Unauthorized")

    def test_the_refusal_does_not_disclose_the_bucket_or_the_allow_list(
        self, resolver
    ):
        """Fixing a status code must not build an enumeration oracle."""
        index, _ = resolver

        with pytest.raises(PermissionError) as excinfo:
            index.handler(
                _event("getFileContents", f"s3://{OTHER_BUCKET}/secret.json"), None
            )

        message = str(excinfo.value)
        assert OTHER_BUCKET not in message
        assert OUTPUT_BUCKET not in message
        assert "secret.json" not in message

    def test_a_refused_bucket_is_never_read(self, resolver, monkeypatch):
        index, _ = resolver
        monkeypatch.setattr(
            index.s3_client,
            "head_object",
            lambda **kw: pytest.fail("read an out-of-allow-list bucket"),
        )
        monkeypatch.setattr(
            index.s3_client,
            "get_object",
            lambda **kw: pytest.fail("read an out-of-allow-list bucket"),
        )

        with pytest.raises(PermissionError):
            index.handler(
                _event("getFileContents", f"s3://{OTHER_BUCKET}/secret.json"), None
            )

    def test_an_absent_object_is_a_client_error_not_a_fault(self, resolver):
        index, _ = resolver

        with pytest.raises(ValueError, match="File not found"):
            index.handler(
                _event("getFileContents", f"s3://{OUTPUT_BUCKET}/nope.json"), None
            )

    def test_an_absent_object_version_is_a_client_error(self, resolver):
        index, _ = resolver

        with pytest.raises(ValueError, match="File not found"):
            index.handler(
                _event(
                    "getFileContents",
                    f"s3://{OUTPUT_BUCKET}/doc/sections/1/result.json",
                    version_id="does-not-exist",
                ),
                None,
            )

    def test_an_accessdenied_stays_a_fault_and_leaks_no_s3_detail(
        self, resolver, monkeypatch
    ):
        """The caller's credentials never touch S3 here, so an AccessDenied is this
        function's own role, the bucket policy or the KMS key — a real fault.

        The raw S3 message is not returned: it distinguished NoSuchBucket from
        AccessDenied from Forbidden (an existence oracle over the allow-listed
        buckets) and S3's text for a denial is literally "Access Denied", which the
        UI's `isAuthorizationError` heuristic reads as the caller's problem.
        """
        index, _ = resolver
        from botocore.exceptions import ClientError

        def _denied(**kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
                "GetObject",
            )

        monkeypatch.setattr(index.s3_client, "get_object", _denied)

        with pytest.raises(Exception) as excinfo:
            index.handler(
                _event("getFileContents", f"s3://{OUTPUT_BUCKET}/x.json"), None
            )

        assert not isinstance(excinfo.value, (PermissionError, ValueError)), (
            "a fault in this deployment's own IAM must not be reported to the "
            "caller as their authorization problem"
        )
        assert "Access Denied" not in str(excinfo.value)
        assert "AccessDenied" not in str(excinfo.value)

    def test_a_403_from_head_object_stays_a_fault(self, resolver, monkeypatch):
        """S3 answers a MISSING key with 403 when the reader lacks ListBucket, so
        this code path is reachable both ways and must not guess."""
        index, _ = resolver
        from botocore.exceptions import ClientError

        def _forbidden(**kwargs):
            raise ClientError(
                {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
            )

        monkeypatch.setattr(index.s3_client, "head_object", _forbidden)

        with pytest.raises(Exception) as excinfo:
            index.handler(
                _event("getFilePresignedUrl", f"s3://{OUTPUT_BUCKET}/x.json"), None
            )

        assert not isinstance(excinfo.value, (PermissionError, ValueError))
        assert "Forbidden" not in str(excinfo.value)


def _resolver_env(logical_id):
    """The `Environment.Variables` map for one function, parsed from the template.

    The template is SAM/CFN, so it carries short-form intrinsics (`!Ref`, `!Sub`,
    `!If`) that a plain safe_load rejects. Resolving them is not the point here — only
    which names are present and whether their values are non-empty — so they are
    loaded as opaque nodes.
    """
    from pathlib import Path

    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    def _passthrough(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return f"!{tag_suffix} {node.value}"
        if isinstance(node, yaml.SequenceNode):
            return [f"!{tag_suffix}"] + loader.construct_sequence(node, deep=True)
        return {f"!{tag_suffix}": loader.construct_mapping(node, deep=True)}

    _Loader.add_multi_constructor("", _passthrough)

    template = Path(__file__).resolve().parents[3] / "template.yaml"
    doc = yaml.load(template.read_text(), Loader=_Loader)  # nosec B506 - custom SafeLoader subclass
    resource = doc["Resources"][logical_id]
    return resource["Properties"]["Environment"]["Variables"]


@pytest.mark.unit
class TestTheAllowListFailsClosed:
    def test_an_unconfigured_allow_list_refuses_every_request(self, monkeypatch):
        """It used to fail OPEN, on the stated premise that an older deployment
        might run this code without the env vars.

        That premise is false: the function's code and its environment variables are
        one CloudFormation resource, updated together. An empty set means a template
        that stopped setting them — a build fault — and reading a build fault as
        "allow every bucket this role can reach" recreates exactly the generic
        S3-read gadget the list exists to prevent.
        """
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
                    _event("getFileContents", f"s3://{OUTPUT_BUCKET}/x.json"), None
                )

    def test_the_template_wires_the_allow_list(self):
        """The premise of the closed case: it must be unreachable in a deployment.

        Fixing a fail-open by making it deny is only safe if nothing real lands in
        the deny branch, and that is a property of the template, not of this file. If
        it ever does, every `getFileContents` call returns 403 and the document
        viewers stop working with copy implying the *caller* is at fault.

        Parsed as YAML rather than sliced between two hardcoded logical ids. A text
        slice that loses its trailing anchor silently widens to the rest of the
        template — where `INPUT_BUCKET:` and `OUTPUT_BUCKET:` both appear on other
        functions — so removing the variables from THIS function and renaming the
        anchor would still have passed. The values are checked non-empty too, because
        `ALLOWED_BUCKETS` filters falsy entries: `INPUT_BUCKET: ""` is wired and still
        fails closed.
        """
        env = _resolver_env("GetFileContentsResolverFunction")

        for required in ("INPUT_BUCKET", "OUTPUT_BUCKET"):
            assert required in env, (
                f"{required} is not set on GetFileContentsResolverFunction, so the "
                "bucket allow-list would be empty and every read refused"
            )
            assert env[required], (
                f"{required} is set to an empty value, which ALLOWED_BUCKETS "
                "filters out — so the allow-list is still empty and every read is "
                "refused"
            )

    def test_every_name_the_code_reads_is_either_wired_or_knowingly_unwired(self):
        """`_ALLOWED_BUCKETS_ENV` names eight variables; the template sets six.

        Not a defect — an unset name simply contributes nothing to the allow-list —
        but it is the kind of drift that makes the set above look bigger than the
        protection actually is, so it is pinned rather than left to be discovered.
        """
        import index

        env = _resolver_env("GetFileContentsResolverFunction")
        wired = {n for n in index._ALLOWED_BUCKETS_ENV if n in env}
        unwired = set(index._ALLOWED_BUCKETS_ENV) - wired

        assert unwired == {"DISCOVERY_BUCKET", "WORKING_BUCKET"}, (
            "the set of bucket env vars the template does not wire has changed: "
            f"{sorted(unwired)}. An object in an unwired bucket is unreachable "
            "through this resolver."
        )
