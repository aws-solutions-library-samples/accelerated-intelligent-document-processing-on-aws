# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``IDPPublisher``'s S3 upload steps: config library, templates,
the version pointer, and the curated sample documents.

Everything here is an S3 key. That is the whole reason the tests are shaped the
way they are: a publish that puts the right bytes at the wrong key reports
success and then fails much later somewhere else — CloudFormation cannot fetch a
nested template, ``ConfigurationCopyFunction`` copies nothing into the stack's
ConfigurationBucket, the Web UI's update indicator resolves the wrong release,
or the Quick Start agent's ``samples-manifest.json`` points at documents that
were never uploaded. None of those failures names the publish step that caused
them.

So the assertions read the fake service back rather than inspecting a mock: the
exact key, the exact bytes, the exact ``ContentType``, and — for every
skip-if-already-present branch — a sentinel body proving the object really was
left alone. The one step that is not an S3 API call at all (``aws s3 sync`` in a
subprocess) has its argv asserted instead, because the destination URI is the
same class of fact.

The sample-file fixtures are deliberately arranged so that the order
``iter_sample_files`` yields differs from the order ``generate_sample_file_list``
returns (a top-level ``rule-validation.pdf`` beside the ``rule-validation/``
batch directory sorts differently before and after the final sort). A fixture
whose walk order already matches the assertion cannot tell a correct
implementation from one that forgot to sort.
"""

from __future__ import annotations

import json
import os
import subprocess
import types

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_sdk._core.publish import IDPPublisher

_BUCKET = "idp-publish-artifacts"
_PREFIX = "idp"
_VERSION = "0.6.9"
_PREFIX_AND_VERSION = f"{_PREFIX}/{_VERSION}"
_REGION = "us-east-1"


def _publisher(s3_client=None, public=False):
    pub = IDPPublisher(verbose=False)
    pub.bucket = _BUCKET
    pub.prefix = _PREFIX
    pub.version = _VERSION
    pub.prefix_and_version = _PREFIX_AND_VERSION
    pub.region = _REGION
    pub.main_template = "idp-main.yaml"
    pub.public = public
    pub.s3_client = s3_client
    pub.console.quiet = True
    return pub


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _keys(s3):
    listed = s3.list_objects_v2(Bucket=_BUCKET)
    return sorted(o["Key"] for o in listed.get("Contents", []))


def _body(s3, key):
    return s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read()


# ---------------------------------------------------------------------------
# upload_config_library
# ---------------------------------------------------------------------------


def _fake_run(monkeypatch, returncode=0, stderr=""):
    """Record ``subprocess.run`` invocations and return a canned result."""
    calls = []

    def fake(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return types.SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake)
    return calls


def test_config_library_sync_targets_the_versioned_prefix(tmp_path, monkeypatch):
    """The whole point of the step is the destination URI it syncs to.

    ``<prefix>/<version>/config_library`` is where ``generate_config_file_list``
    tells the deploy-time ``ConfigurationCopyFunction`` to look. A sync to the
    version-free prefix, or to a differently-spelled subfolder, leaves a fresh
    stack with an empty ConfigurationBucket and no error anywhere.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "config_library" / "pricing.yaml", "units: {}\n")
    _write(
        tmp_path / "config_library" / "unified" / "rvl-cdip" / "config.yaml", "a: 1\n"
    )
    calls = _fake_run(monkeypatch)

    _publisher().upload_config_library()

    assert len(calls) == 1
    assert calls[0]["cmd"] == [
        "aws",
        "s3",
        "sync",
        "config_library",
        f"s3://{_BUCKET}/{_PREFIX_AND_VERSION}/config_library",
        "--region",
        _REGION,
    ]
    # stderr has to be captured or the failure message below would be empty.
    assert calls[0]["kwargs"]["capture_output"] is True
    assert calls[0]["kwargs"]["text"] is True


def test_config_library_sync_is_skipped_when_the_directory_is_absent(
    tmp_path, monkeypatch
):
    """A trimmed checkout warns and returns instead of shelling out."""
    monkeypatch.chdir(tmp_path)
    calls = _fake_run(monkeypatch)

    assert _publisher().upload_config_library() is None
    assert calls == []


def test_a_failed_config_library_sync_aborts_publish_and_shows_stderr(
    tmp_path, monkeypatch, capsys
):
    """A non-zero exit from the CLI must stop publish and surface the reason.

    Continuing would publish a template whose ``<CONFIG_FILES_LIST_TOKEN>``
    names objects that are not in the bucket.
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "config_library" / "pricing.yaml", "units: {}\n")
    _fake_run(monkeypatch, returncode=1, stderr="AccessDenied on PutObject")
    pub = _publisher()
    pub.console.quiet = False

    with pytest.raises(SystemExit) as exc:
        pub.upload_config_library()

    assert exc.value.code == 1
    assert "AccessDenied on PutObject" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _upload_template_to_s3
# ---------------------------------------------------------------------------


@mock_aws
def test_a_template_is_uploaded_byte_for_byte_at_the_given_key(
    tmp_path, monkeypatch, aws_credentials
):
    """CloudFormation fetches the object by key, so both must be exact.

    A template whose bytes were re-encoded or whose key drifted produces a
    ``ValidationError``/404 at stack-create time, minutes after publish reported
    success.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    template = tmp_path / "nested" / "api-resolvers" / ".aws-sam" / "packaged.yaml"
    _write(template, "AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n")
    pub = _publisher(s3)

    key = f"{_PREFIX_AND_VERSION}/api-resolvers.yaml"
    pub._upload_template_to_s3(str(template), key, "API resolvers template")

    assert _keys(s3) == [key]
    assert _body(s3, key) == template.read_bytes()


@mock_aws
def test_a_template_upload_failure_aborts_publish(
    tmp_path, monkeypatch, aws_credentials
):
    """Unlike the packaging steps, this handler catches any exception.

    Exercised with a path that does not exist, which is what a rename or a
    missing ``sam package`` output looks like. The bare ``except Exception`` here
    is the shape the packaging methods' ``except ClientError`` should have had.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    with pytest.raises(SystemExit) as exc:
        pub._upload_template_to_s3(
            "does/not/exist.yaml", "k/ey.yaml", "Nested template"
        )

    assert exc.value.code == 1
    assert _keys(s3) == []


# ---------------------------------------------------------------------------
# version_pointer_key / _upload_version_pointer
# ---------------------------------------------------------------------------


def test_the_version_pointer_key_is_version_free_and_derived_from_the_template():
    """The pointer must live at the version-free prefix, or it cannot be found.

    The Web UI resolver GetObjects one known key with no bucket listing, so the
    key has to be a constant for the bucket — ``<prefix>/idp-main-latest.json``,
    a sibling of the main template rather than a child of
    ``<prefix>/<version>/``. Putting it under the version would make the "update
    available" indicator resolvable only by a client that already knew the
    newest version.
    """
    pub = _publisher()

    assert pub.version_pointer_key() == f"{_PREFIX}/idp-main-latest.json"
    assert _VERSION not in pub.version_pointer_key()

    pub.main_template = "idp-main-govcloud.yaml"
    assert pub.version_pointer_key() == f"{_PREFIX}/idp-main-govcloud-latest.json"


@mock_aws
def test_the_pointer_body_names_the_versioned_template_that_publish_uploads(
    tmp_path, monkeypatch, aws_credentials
):
    """``templateUrl`` must match the key the versioned main template lands at.

    The pointer's URL and the upload key are computed by two separate
    expressions in ``publish.py`` (``_upload_version_pointer`` builds
    ``<basename>_<version>.yaml``; ``set_public_acls`` and the summary use
    ``main_template.replace('.yaml', f'_{version}.yaml')``). If they ever
    disagree the indicator offers an upgrade to a template that 404s, so this
    asserts the pointer against the other expression rather than against a
    literal.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    pub._upload_version_pointer()

    body = json.loads(_body(s3, pub.version_pointer_key()))
    expected_key = (
        f"{_PREFIX}/{pub.main_template.replace('.yaml', f'_{_VERSION}.yaml')}"
    )
    assert body == {
        "version": _VERSION,
        "templateUrl": f"https://s3.{_REGION}.amazonaws.com/{_BUCKET}/{expected_key}",
    }
    assert (
        s3.get_object(Bucket=_BUCKET, Key=pub.version_pointer_key())["ContentType"]
        == "application/json"
    )


class _AclRejectingClient:
    """A thin wrapper that refuses any ACL argument, then delegates.

    Models an artifacts bucket with Object Ownership = BucketOwnerEnforced.
    ``moto`` 5.1.8 accepts ``ACL=`` on such a bucket (verified), so the rejection
    has to come from the wrapper — but the accepted call still reaches the real
    fake, so the body and key written on the fallback path are read back from S3
    rather than taken on trust from a mock's call args.
    """

    def __init__(self, inner, code):
        self._inner = inner
        self._code = code
        self.rejected = 0

    def put_object(self, **kwargs):
        if "ACL" in kwargs:
            self.rejected += 1
            raise ClientError(
                {"Error": {"Code": self._code, "Message": "ACLs are not supported"}},
                "PutObject",
            )
        return self._inner.put_object(**kwargs)


@mock_aws
@pytest.mark.parametrize(
    "code", ["AccessControlListNotSupported", "InvalidBucketAclWithObjectOwnership"]
)
def test_a_bucket_that_rejects_acls_still_gets_the_pointer(
    code, tmp_path, monkeypatch, aws_credentials
):
    """Losing the ACL must not mean losing the pointer.

    A bucket with BucketOwnerEnforced grants anonymous read by policy instead, so
    the write is retried without the ACL. Dropping the object instead would
    disable the update indicator for every such release bucket — the same
    failure mode as #962, arrived at from the other direction.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(_AclRejectingClient(s3, code), public=True)

    pub._upload_version_pointer()

    assert pub.s3_client.rejected == 1
    assert json.loads(_body(s3, pub.version_pointer_key()))["version"] == _VERSION


@mock_aws
def test_an_unrelated_client_error_leaves_no_pointer_and_does_not_raise(
    tmp_path, monkeypatch, aws_credentials
):
    """The pointer is best-effort: a failure warns and publish continues.

    The indicator is a convenience, so an error writing it must not abort a
    publish whose templates and layers are already uploaded. Driven here by a
    bucket that does not exist (``NoSuchBucket``), which is outside the two
    ACL-specific codes the inner handler tolerates.
    """
    monkeypatch.chdir(tmp_path)
    pub = _publisher(boto3.client("s3", region_name=_REGION), public=True)

    assert pub._upload_version_pointer() is None


@mock_aws
def test_a_private_publish_does_not_retry_without_an_acl(
    tmp_path, monkeypatch, aws_credentials
):
    """With ``public=False`` no ACL is sent, so there is nothing to retry.

    The tolerance branch is guarded by ``self.public``; a private publish that
    fails for an ACL-shaped reason has hit something else, and swallowing it into
    a retry would hide that.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    wrapper = _AclRejectingClient(s3, "AccessControlListNotSupported")
    pub = _publisher(wrapper, public=False)

    pub._upload_version_pointer()

    assert wrapper.rejected == 0
    assert json.loads(_body(s3, pub.version_pointer_key()))["version"] == _VERSION


# ---------------------------------------------------------------------------
# _check_and_upload_template
# ---------------------------------------------------------------------------


@mock_aws
def test_an_existing_template_object_is_not_overwritten(
    tmp_path, monkeypatch, aws_credentials
):
    """The check exists so a re-publish of the same version is cheap.

    Read the sentinel back: an assertion that merely "did not raise" would pass
    if the object had been replaced.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    key = f"{_PREFIX_AND_VERSION}/pattern-unified.yaml"
    s3.put_object(Bucket=_BUCKET, Key=key, Body=b"already published")
    local = tmp_path / "packaged.yaml"
    _write(local, "Resources: {}\n")

    _publisher(s3)._check_and_upload_template(str(local), key, "Unified pattern")

    assert _body(s3, key) == b"already published"


@mock_aws
def test_a_missing_template_object_is_uploaded(tmp_path, monkeypatch, aws_credentials):
    """A 404 is the repair path: the local file is uploaded at that key."""
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    local = tmp_path / "packaged.yaml"
    _write(local, "Resources:\n  Fn: {}\n")
    key = f"{_PREFIX_AND_VERSION}/pattern-unified.yaml"

    _publisher(s3)._check_and_upload_template(str(local), key, "Unified pattern")

    assert _keys(s3) == [key]
    assert _body(s3, key) == local.read_bytes()


@mock_aws
def test_a_missing_object_with_no_local_template_aborts_publish(
    tmp_path, monkeypatch, aws_credentials
):
    """Neither in S3 nor on disk means the stack cannot deploy — fail now.

    This is the one case that must not be tolerated: the nested template would
    simply be absent, and CloudFormation would report a 404 on
    ``TemplateURL`` to whoever launched the stack.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    with pytest.raises(SystemExit) as exc:
        _publisher(s3)._check_and_upload_template(
            "nested/gone/.aws-sam/packaged.yaml",
            f"{_PREFIX_AND_VERSION}/gone.yaml",
            "Gone template",
        )

    assert exc.value.code == 1


@mock_aws
def test_an_unreadable_bucket_warns_and_continues_without_uploading(
    tmp_path, monkeypatch, aws_credentials
):
    """A non-404 error is tolerated here, and that is worth knowing.

    ``_check_and_upload_template`` prints a warning and returns — it does not
    exit, and it does not upload. So if the existence check fails for any reason
    other than "not found" (a denied ``s3:ListBucket``, a bucket in another
    region), publish carries on and the template is never uploaded. The three
    packaging methods make the opposite choice on the same condition and exit 1.
    Pinned as-is because it is reachable from a same-account publish with a
    read-restricted role.
    """
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "packaged.yaml"
    _write(local, "Resources: {}\n")
    pub = _publisher(boto3.client("s3", region_name=_REGION))

    assert (
        pub._check_and_upload_template(
            str(local), f"{_PREFIX_AND_VERSION}/x.yaml", "Nested"
        )
        is None
    )


# ---------------------------------------------------------------------------
# iter_sample_files / generate_sample_file_list
# ---------------------------------------------------------------------------


def _samples_tree(root):
    """A samples/ tree covering every selection rule, in an awkward order.

    ``rule-validation.pdf`` sits beside the ``rule-validation/`` batch directory
    on purpose: ``.`` (0x2E) sorts before ``/`` (0x2F), so the final sorted list
    puts the file first while the directory walk yields it second. That is what
    makes the difference between ``iter_sample_files`` and
    ``generate_sample_file_list`` observable.
    """
    s = root / "samples"
    _write(s / "alpha.PDF", "%PDF upper-case extension")
    _write(s / "zebra.webp", "webp bytes")
    _write(s / "report.xlsx", "not a document")
    _write(s / "rule-validation.pdf", "%PDF top level")
    _write(s / "rule-validation" / "rv_a.tiff", "tiff bytes")
    _write(s / "rule-validation" / "notes.md", "prose")
    _write(s / "w2" / "W2_10.pdf", "%PDF ten")
    _write(s / "w2" / "W2_2.pdf", "%PDF two")
    _write(s / "w2" / "nested" / "deep.pdf", "%PDF nested")
    _write(s / "external-mcp-client" / "demo.pdf", "%PDF in a code sample dir")
    return s


def test_iter_sample_files_yields_directory_walk_order(tmp_path, monkeypatch):
    """The generator applies the selection rules without sorting the result.

    Every rule is exercised by the fixture: an upper-case extension is accepted,
    a non-document extension is dropped, an unknown subdirectory is dropped
    whole (this is what keeps ``external-mcp-client/`` and the lambda-hook
    samples out of the release), and a batch directory is read one level deep
    only, so ``w2/nested/deep.pdf`` does not ship.
    """
    monkeypatch.chdir(tmp_path)
    _samples_tree(tmp_path)

    assert list(_publisher().iter_sample_files()) == [
        "alpha.PDF",
        "rule-validation/rv_a.tiff",
        "rule-validation.pdf",
        "w2/W2_10.pdf",
        "w2/W2_2.pdf",
        "zebra.webp",
    ]


def test_generate_sample_file_list_sorts_what_the_walk_yields(tmp_path, monkeypatch):
    """The list is baked into the template as ``<SAMPLE_FILES_LIST_TOKEN>``.

    An unsorted list would churn between publishes on filesystems whose
    ``listdir`` order differs, which changes the template's bytes and forces a
    needless ``CopySampleFiles`` custom-resource re-run on every deploy. Note the
    result is NOT the walk order above — ``rule-validation.pdf`` moves ahead of
    ``rule-validation/rv_a.tiff``.
    """
    monkeypatch.chdir(tmp_path)
    _samples_tree(tmp_path)

    assert _publisher().generate_sample_file_list() == [
        "alpha.PDF",
        "rule-validation.pdf",
        "rule-validation/rv_a.tiff",
        "w2/W2_10.pdf",
        "w2/W2_2.pdf",
        "zebra.webp",
    ]


def test_no_samples_directory_yields_nothing(tmp_path, monkeypatch):
    """A trimmed checkout produces an empty list, not an exception."""
    monkeypatch.chdir(tmp_path)
    pub = _publisher()

    assert list(pub.iter_sample_files()) == []
    assert pub.generate_sample_file_list() == []


def test_an_empty_batch_directory_contributes_nothing(tmp_path, monkeypatch):
    """A batch directory holding no documents adds no entries."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "samples" / "w2").mkdir(parents=True)
    _write(tmp_path / "samples" / "w2" / "README.md", "empty for now")

    assert _publisher().generate_sample_file_list() == []


def test_the_file_list_matches_the_manifest_s3_keys(tmp_path, monkeypatch):
    """The copy list and the manifest must describe the same objects.

    ``samples-manifest.json`` tells the Quick Start agent to open
    ``samples/<rel>``; ``generate_sample_file_list`` is what actually gets copied
    into the stack's ConfigurationBucket. They are produced by two separate walks
    over ``samples/`` (``generate_samples_manifest`` and ``iter_sample_files``),
    so a rule added to one and not the other gives the agent a manifest entry
    whose document is not there. This asserts the two walks agree.
    """
    monkeypatch.chdir(tmp_path)
    _samples_tree(tmp_path)
    (tmp_path / "config_library").mkdir()
    pub = _publisher()

    manifest = pub.generate_samples_manifest()
    from_manifest = set()
    for sample in manifest["samples"]:
        if sample["kind"] == "batch":
            from_manifest.update(sample["files"])
        else:
            from_manifest.add(sample["s3Key"])

    assert from_manifest == {
        f"samples/{rel}" for rel in pub.generate_sample_file_list()
    }


# ---------------------------------------------------------------------------
# upload_samples
# ---------------------------------------------------------------------------


@mock_aws
def test_samples_are_uploaded_under_the_versioned_samples_prefix(
    tmp_path, monkeypatch, aws_credentials
):
    """Keys must be ``<prefix>/<version>/samples/<rel>``, batch nesting intact.

    ``CopySampleFiles`` copies each of these into the stack's
    ConfigurationBucket at ``samples/<rel>``, which is exactly where the
    manifest's ``s3Key`` points. Flattening ``w2/W2_2.pdf`` to ``W2_2.pdf``, or
    dropping the version from the prefix, breaks that chain silently.
    """
    monkeypatch.chdir(tmp_path)
    samples = _samples_tree(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)

    pub.upload_samples()

    assert _keys(s3) == [
        f"{_PREFIX_AND_VERSION}/samples/alpha.PDF",
        f"{_PREFIX_AND_VERSION}/samples/rule-validation.pdf",
        f"{_PREFIX_AND_VERSION}/samples/rule-validation/rv_a.tiff",
        f"{_PREFIX_AND_VERSION}/samples/w2/W2_10.pdf",
        f"{_PREFIX_AND_VERSION}/samples/w2/W2_2.pdf",
        f"{_PREFIX_AND_VERSION}/samples/zebra.webp",
    ]
    # The binaries themselves, not just the keys: a wrong local path would put
    # one document's bytes under another's key.
    assert (
        _body(s3, f"{_PREFIX_AND_VERSION}/samples/w2/W2_2.pdf")
        == (samples / "w2" / "W2_2.pdf").read_bytes()
    )
    assert (
        _body(s3, f"{_PREFIX_AND_VERSION}/samples/rule-validation.pdf")
        == (samples / "rule-validation.pdf").read_bytes()
    )


@mock_aws
def test_no_samples_uploads_nothing(tmp_path, monkeypatch, aws_credentials):
    """With nothing to upload the step is a no-op, not an error."""
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    assert _publisher(s3).upload_samples() is None
    assert _keys(s3) == []


# ---------------------------------------------------------------------------
# _upload_samples_manifest_to_artifacts / _upload_catalog_to_artifacts
# ---------------------------------------------------------------------------


@mock_aws
def test_the_samples_manifest_is_uploaded_as_json_under_config_library(
    tmp_path, monkeypatch, aws_credentials
):
    """It must land inside the same ``config_library/`` prefix the sync used.

    ``upload_config_library`` has already run by this point, so the freshly
    generated manifest is uploaded on its own. It only reaches the stack because
    ``generate_config_file_list`` walks the *local* ``config_library/`` and the
    copy function resolves each entry against ``<prefix>/<version>/config_library/``
    — so any other key makes the copy 404.
    """
    monkeypatch.chdir(tmp_path)
    _samples_tree(tmp_path)
    (tmp_path / "config_library").mkdir()
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)
    manifest = pub.generate_samples_manifest()

    pub._upload_samples_manifest_to_artifacts()

    key = f"{_PREFIX_AND_VERSION}/config_library/samples-manifest.json"
    assert _keys(s3) == [key]
    obj = s3.get_object(Bucket=_BUCKET, Key=key)
    assert obj["ContentType"] == "application/json"
    assert json.loads(obj["Body"].read()) == manifest


@mock_aws
def test_the_samples_manifest_upload_is_a_no_op_when_it_was_never_written(
    tmp_path, monkeypatch, aws_credentials
):
    """No ``samples/`` means no manifest, and the upload must skip quietly.

    ``generate_samples_manifest`` returns None without writing a file when
    ``samples/`` is absent, so this step has to tolerate its own input being
    missing rather than raising from inside the publish sequence.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config_library").mkdir()
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    assert _publisher(s3)._upload_samples_manifest_to_artifacts() is None
    assert _keys(s3) == []


@mock_aws
def test_the_catalog_is_uploaded_as_json_under_config_library(
    tmp_path, monkeypatch, aws_credentials
):
    """The host's ``listCatalogFeatures`` resolver reads this one object.

    Same mechanism as the manifest above, and the same failure if the key drifts:
    the feature catalog silently lists nothing, so no extension is installable.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config_library").mkdir()
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)
    catalog = pub.write_catalog_file(
        [
            {
                "featureId": "sample-feature",
                "displayName": "Sample Feature",
                "description": "",
                "iconUrl": "",
                "source": "oss",
                "latestVersion": "1.0.0",
            }
        ]
    )

    pub._upload_catalog_to_artifacts()

    key = f"{_PREFIX_AND_VERSION}/config_library/catalog.json"
    assert _keys(s3) == [key]
    obj = s3.get_object(Bucket=_BUCKET, Key=key)
    assert obj["ContentType"] == "application/json"
    assert json.loads(obj["Body"].read()) == catalog


@mock_aws
def test_the_catalog_upload_is_a_no_op_when_no_catalog_was_written(
    tmp_path, monkeypatch, aws_credentials
):
    """A checkout with no ``config_library/`` uploads nothing and does not raise."""
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    assert _publisher(s3)._upload_catalog_to_artifacts() is None
    assert _keys(s3) == []


@mock_aws
def test_the_manifest_and_catalog_share_the_config_library_prefix(
    tmp_path, monkeypatch, aws_credentials
):
    """Both late uploads must sit under the prefix the bulk sync wrote to.

    They are separate methods with separately-spelled keys, and both depend on
    landing beside the synced tree. Asserting them together is what catches one
    of the two being changed.
    """
    monkeypatch.chdir(tmp_path)
    _samples_tree(tmp_path)
    (tmp_path / "config_library").mkdir()
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)
    pub = _publisher(s3)
    pub.generate_samples_manifest()
    pub.write_catalog_file([])

    pub._upload_samples_manifest_to_artifacts()
    pub._upload_catalog_to_artifacts()

    prefix = f"{_PREFIX_AND_VERSION}/config_library/"
    assert _keys(s3) == [
        f"{prefix}catalog.json",
        f"{prefix}samples-manifest.json",
    ]
    assert all(k.startswith(prefix) for k in _keys(s3))
    assert not any(os.sep + os.sep in k for k in _keys(s3))
