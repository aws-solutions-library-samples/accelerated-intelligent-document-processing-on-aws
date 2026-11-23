# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Terraform module versions index, against moto's S3.

Publishing writes ``<prefix>/terraform/idp-terraform-versions.json`` beside the
latest pointer, with one entry per published version; a deploy can pin a
version through it. moto implements S3's conditional writes (``If-Match`` and
``If-None-Match`` answer ``412 PreconditionFailed``), which is what the
concurrency tests rely on. That is moto's behaviour, not a measurement of S3's.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from idp_sdk._core import terraform_deploy as tf
from idp_sdk.exceptions import IDPConfigurationError, IDPStackError

pytestmark = pytest.mark.unit

REGION = "us-west-2"
BUCKET = "tfindex-111122223333-us-west-2"
PREFIX = "artifacts/genai-idp"
INDEX_KEY = f"{PREFIX}/terraform/idp-terraform-versions.json"
POINTER_KEY = f"{PREFIX}/terraform/idp-terraform-latest.json"


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-for-moto")  # nosec B105
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield client


def _module(tmp_path: Path, text: str) -> Path:
    root = tmp_path / f"module-{hashlib.sha256(text.encode()).hexdigest()[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.tf").write_text(text)
    (root / tf.MANIFEST_NAME).write_text(json.dumps({"parameters": {}}))
    return root


def _publish(s3, tmp_path, version, text="# module\n", **kw):
    return tf.upload_module(
        s3,
        module_dir=_module(tmp_path, text),
        bucket=BUCKET,
        prefix=PREFIX,
        region=REGION,
        version=version,
        release_date=kw.pop("release_date", "2026-11-20"),
        **kw,
    )


def _read(s3, key):
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _index(s3):
    return json.loads(_read(s3, INDEX_KEY))


def _versions(s3):
    return [e["version"] for e in _index(s3)["versions"]]


# --------------------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------------------


class TestPublish:
    def test_the_first_publish_creates_the_index(self, s3, tmp_path):
        info = _publish(s3, tmp_path, "0.6.15")
        zip_key = f"{PREFIX}/0.6.15/terraform/idp-terraform-0.6.15.zip"
        assert _index(s3) == {
            "schemaVersion": 1,
            "versions": [
                {
                    "version": "0.6.15",
                    "moduleKey": zip_key,
                    "sha256": hashlib.sha256(_read(s3, zip_key)).hexdigest(),
                    "releaseDate": "2026-11-20",
                }
            ],
        }
        assert info["versionsIndexUrl"] == tf.s3_https_url(REGION, BUCKET, INDEX_KEY)
        # The pointer keeps its format and is still written.
        assert json.loads(_read(s3, POINTER_KEY))["version"] == "0.6.15"

    def test_the_release_date_defaults_to_today_utc(self, s3, tmp_path):
        tf.upload_module(
            s3,
            module_dir=_module(tmp_path, "x"),
            bucket=BUCKET,
            prefix=PREFIX,
            region=REGION,
            version="0.6.15",
        )
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        assert _index(s3)["versions"][0]["releaseDate"] == today

    def test_later_publishes_append_and_never_drop_an_entry(self, s3, tmp_path):
        _publish(s3, tmp_path, "0.6.15", "a")
        _publish(s3, tmp_path, "0.6.9", "b")
        _publish(s3, tmp_path, "0.6.16.dev1", "c")
        _publish(s3, tmp_path, "0.6.10", "d")
        # Numeric order, not string order (0.6.9 before 0.6.10).
        assert _versions(s3) == ["0.6.9", "0.6.10", "0.6.15", "0.6.16.dev1"]
        # The pointer names the last publish, whatever its version.
        assert json.loads(_read(s3, POINTER_KEY))["version"] == "0.6.10"

    def test_an_identical_republish_leaves_the_index_untouched(self, s3, tmp_path):
        _publish(s3, tmp_path, "0.6.15", "same")
        etag = s3.head_object(Bucket=BUCKET, Key=INDEX_KEY)["ETag"]
        _publish(s3, tmp_path, "0.6.15", "same", release_date="2027-01-01")
        assert s3.head_object(Bucket=BUCKET, Key=INDEX_KEY)["ETag"] == etag
        assert _index(s3)["versions"][0]["releaseDate"] == "2026-11-20"

    def test_a_release_republished_with_other_bytes_is_refused_before_upload(
        self, s3, tmp_path
    ):
        _publish(s3, tmp_path, "0.6.15", "original")
        zip_key = f"{PREFIX}/0.6.15/terraform/idp-terraform-0.6.15.zip"
        before = {
            k: _read(s3, k)
            for k in (zip_key, zip_key + ".sha256", INDEX_KEY, POINTER_KEY)
        }
        with pytest.raises(IDPStackError, match=r"0\.6\.15 is already published"):
            _publish(s3, tmp_path, "0.6.15", "changed")
        # Nothing was overwritten: not the zip, its checksum, the index or pointer.
        assert {k: _read(s3, k) for k in before} == before

    def test_a_pre_release_republished_with_other_bytes_replaces_its_entry(
        self, s3, tmp_path
    ):
        _publish(s3, tmp_path, "0.6.15", "release")
        _publish(s3, tmp_path, "0.6.16.dev1", "first")
        info = _publish(s3, tmp_path, "0.6.16.dev1", "second")
        entries = {e["version"]: e for e in _index(s3)["versions"]}
        assert list(entries) == ["0.6.15", "0.6.16.dev1"]
        assert entries["0.6.16.dev1"]["sha256"] == info["sha256"]

    def test_a_variant_has_an_index_of_its_own(self, s3, tmp_path):
        _publish(s3, tmp_path, "0.6.15", "std")
        _publish(s3, tmp_path, "0.6.15", "headless", variant="headless")
        variant_index = json.loads(
            _read(s3, f"{PREFIX}/terraform/idp-terraform-headless-versions.json")
        )
        assert variant_index["versions"][0]["moduleKey"].endswith(
            "/idp-terraform-headless-0.6.15.zip"
        )
        assert len(_index(s3)["versions"]) == 1

    def test_the_index_is_written_before_the_pointer(self, s3, tmp_path):
        """So a reader never sees a pointer naming a version the index lacks."""
        order = []
        s3.meta.events.register(
            "provide-client-params.s3.PutObject",
            lambda params, **_: order.append(params["Key"].rsplit("/", 1)[1]),
        )
        _publish(s3, tmp_path, "0.6.15")
        assert order.index("idp-terraform-versions.json") < order.index(
            "idp-terraform-latest.json"
        )


# --------------------------------------------------------------------------------------
# Concurrent publishes
# --------------------------------------------------------------------------------------


def _interleave(s3, times=1):
    """Before each of the first ``times`` index writes ``s3`` makes, another
    publisher (a second client) adds a version of its own. Returns the
    versions that publisher added."""
    other = boto3.client("s3", region_name=REGION)
    added = []

    def hook(params, **_):
        if params.get("Key") != INDEX_KEY or len(added) >= times:
            return
        version = f"9.9.{len(added)}"
        added.append(version)
        entry = tf.build_index_entry(version, f"k/{version}.zip", "0" * 64, "x")
        tf.update_versions_index(other, bucket=BUCKET, key=INDEX_KEY, entry=entry)

    s3.meta.events.register("provide-client-params.s3.PutObject", hook)
    return added


class TestConcurrency:
    def test_a_write_racing_an_existing_index_is_retried_not_lost(self, s3, tmp_path):
        _publish(s3, tmp_path, "0.6.14", "old")
        added = _interleave(s3)
        _publish(s3, tmp_path, "0.6.15", "new")
        assert added == ["9.9.0"]
        assert _versions(s3) == ["0.6.14", "0.6.15", "9.9.0"]

    def test_two_publishers_creating_the_index_both_land(self, s3, tmp_path):
        """Neither sees an index; the second create fails If-None-Match and is
        redone as an update of what the first wrote."""
        added = _interleave(s3)
        _publish(s3, tmp_path, "0.6.15")
        assert added == ["9.9.0"]
        assert _versions(s3) == ["0.6.15", "9.9.0"]

    def test_the_immutability_check_holds_under_the_race_too(self, s3, tmp_path):
        """A concurrent publish of the same release with other bytes, landing
        between the pre-check and the write, is refused on the retry."""
        other = boto3.client("s3", region_name=REGION)
        fired = []

        def hook(params, **_):
            if params.get("Key") == INDEX_KEY and not fired:
                fired.append(1)
                entry = tf.build_index_entry("0.6.15", "k", "f" * 64, "x")
                tf.update_versions_index(
                    other, bucket=BUCKET, key=INDEX_KEY, entry=entry
                )

        s3.meta.events.register("provide-client-params.s3.PutObject", hook)
        with pytest.raises(IDPStackError, match="already published"):
            _publish(s3, tmp_path, "0.6.15")
        assert [e["sha256"] for e in _index(s3)["versions"]] == ["f" * 64]

    def test_a_publish_that_loses_every_race_fails_instead_of_overwriting(
        self, s3, tmp_path
    ):
        _publish(s3, tmp_path, "0.6.14", "old")
        added = _interleave(s3, times=tf.VERSIONS_INDEX_ATTEMPTS)
        with pytest.raises(IDPStackError, match="changed under every one"):
            _publish(s3, tmp_path, "0.6.15", "new")
        assert len(added) == tf.VERSIONS_INDEX_ATTEMPTS
        assert "0.6.15" not in _versions(s3)
        assert set(added) <= set(_versions(s3))

    def test_a_botocore_without_conditional_writes_is_refused(self, s3):
        class _NoConditions:
            def __init__(self, real):
                self._real = real

            def operation_model(self, name):
                op = self._real.operation_model(name)

                class _Shape:
                    members = {
                        k: v
                        for k, v in op.input_shape.members.items()
                        if k not in ("IfMatch", "IfNoneMatch")
                    }

                class _Op:
                    input_shape = _Shape

                return _Op

        old = SimpleNamespace(
            meta=SimpleNamespace(service_model=_NoConditions(s3.meta.service_model)),
            get_object=s3.get_object,
            put_object=s3.put_object,
        )
        entry = tf.build_index_entry("1.0.0", "k", "0" * 64, "x")
        with pytest.raises(IDPConfigurationError, match="conditional writes"):
            tf.update_versions_index(old, bucket=BUCKET, key=INDEX_KEY, entry=entry)
        assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


# --------------------------------------------------------------------------------------
# Backfill: releases published before the index existed
# --------------------------------------------------------------------------------------


def _legacy_release(s3, version, body=b"zip", digest=None, variant=None):
    """What a publish made before the index existed: the zip and its checksum."""
    key = tf.module_zip_key(PREFIX, version, variant)
    s3.put_object(Bucket=BUCKET, Key=key, Body=body)
    digest = digest if digest is not None else hashlib.sha256(body).hexdigest()
    s3.put_object(
        Bucket=BUCKET,
        Key=key + ".sha256",
        Body=tf.sha256_file_body(digest, key.rsplit("/", 1)[1]).encode(),
    )
    return key, digest


class TestBackfill:
    def test_a_new_index_lists_the_releases_already_in_the_bucket(self, s3, tmp_path):
        k13, d13 = _legacy_release(s3, "0.6.13", b"a")
        k14, d14 = _legacy_release(s3, "0.6.14", b"b")
        # Not modules: a version directory with only templates, the pointer's
        # own directory, a malformed checksum, and another variant's zip.
        s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/0.6.12/idp-main.yaml", Body=b"")
        s3.put_object(Bucket=BUCKET, Key=POINTER_KEY, Body=b"{}")
        _legacy_release(s3, "0.6.11", digest="not-a-digest")
        _legacy_release(s3, "0.6.10", variant="headless")
        _publish(s3, tmp_path, "0.6.15")
        entries = {e["version"]: e for e in _index(s3)["versions"]}
        assert list(entries) == ["0.6.13", "0.6.14", "0.6.15"]
        assert entries["0.6.13"]["moduleKey"] == k13
        assert entries["0.6.13"]["sha256"] == d13
        assert entries["0.6.14"]["sha256"] == d14
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        assert entries["0.6.14"]["releaseDate"] == today

    def test_backfill_runs_only_when_the_index_is_created(self, s3, tmp_path):
        _publish(s3, tmp_path, "0.6.14", "x")
        _legacy_release(s3, "0.6.13", b"late")
        _publish(s3, tmp_path, "0.6.15", "y")
        assert _versions(s3) == ["0.6.14", "0.6.15"]

    def test_a_create_race_between_two_backfilling_publishers_has_no_duplicates(
        self, s3, tmp_path
    ):
        """Both publishers see no index and both backfill. The one that loses
        the create merges into the winner's index, which already holds the
        backfill, instead of adding its own copy of it."""
        _legacy_release(s3, "0.6.13", b"a")
        other = boto3.client("s3", region_name=REGION)
        # The other publisher backfilled before this one's zip landed, so its
        # index does not hold this publish's entry yet.
        seeded = tf.discover_published_modules(other, BUCKET, PREFIX)
        fired = []

        def hook(params, **_):
            if params.get("Key") == INDEX_KEY and not fired:
                fired.append(1)
                entry = tf.build_index_entry("0.6.14", "k", "0" * 64, "x")
                tf.update_versions_index(
                    other,
                    bucket=BUCKET,
                    key=INDEX_KEY,
                    entry=entry,
                    seed=lambda: seeded,
                )

        s3.meta.events.register("provide-client-params.s3.PutObject", hook)
        _publish(s3, tmp_path, "0.6.15")
        assert fired
        assert _versions(s3) == ["0.6.13", "0.6.14", "0.6.15"]

    def test_a_backfilled_release_is_immutable_too(self, s3, tmp_path):
        _legacy_release(s3, "0.6.15", b"the released bytes")
        with pytest.raises(IDPStackError, match="already published"):
            _publish(s3, tmp_path, "0.6.15", "other")
        assert _read(s3, tf.module_zip_key(PREFIX, "0.6.15")) == b"the released bytes"


# --------------------------------------------------------------------------------------
# Resolving a version (deploy)
# --------------------------------------------------------------------------------------


class TestResolve:
    def _pointer_url(self, bucket=BUCKET):
        return tf.s3_https_url(REGION, bucket, POINTER_KEY)

    def test_a_version_resolves_to_its_immutable_key_and_checksum(self, s3, tmp_path):
        old = _publish(s3, tmp_path, "0.6.14", "old")
        _publish(s3, tmp_path, "0.6.15", "new")
        url, sha = tf.resolve_module_source(None, self._pointer_url(), "0.6.14")
        assert (url, sha) == (old["moduleUrl"], old["sha256"])
        # The bytes at that URL are the ones the index recorded.
        manifest = tf.load_module_manifest(url, sha)
        assert manifest == {"parameters": {}}

    def test_without_a_version_the_pointer_is_used_as_before(self, s3, tmp_path):
        _publish(s3, tmp_path, "0.6.14", "old")
        new = _publish(s3, tmp_path, "0.6.15", "new")
        assert tf.resolve_module_source(None, self._pointer_url()) == (
            new["moduleUrl"],
            new["sha256"],
        )
        assert tf.resolve_module_source("https://x.test/m.zip", None) == (
            "https://x.test/m.zip",
            None,
        )

    def test_bytes_changed_after_publish_are_refused(self, s3, tmp_path):
        info = _publish(s3, tmp_path, "0.6.14")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(tf.MANIFEST_NAME, "{}")
        key = tf.module_zip_key(PREFIX, "0.6.14")
        s3.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue())
        url, sha = tf.resolve_module_source(None, self._pointer_url(), "0.6.14")
        assert sha == info["sha256"]
        with pytest.raises(IDPConfigurationError, match="sha256"):
            tf.load_module_manifest(url, sha)

    def test_the_url_is_in_the_index_buckets_own_copy(self):
        """A replicated bucket's index names keys, not another bucket's URL."""
        index = {
            "schemaVersion": 1,
            "versions": [tf.build_index_entry("1.0.0", "p/1.zip", "a" * 64, "d")],
        }
        url, _ = tf.resolve_index_version(
            index, "1.0.0", "https://s3.ca-central-1.amazonaws.com/replica/p/i.json"
        )
        assert url == "https://s3.ca-central-1.amazonaws.com/replica/p/1.zip"

    def test_an_unknown_version_names_the_published_ones(self, s3, tmp_path):
        _publish(s3, tmp_path, "0.6.14")
        with pytest.raises(IDPConfigurationError, match=r"0\.9\.9.*0\.6\.14"):
            tf.resolve_module_source(None, self._pointer_url(), "0.9.9")

    @pytest.mark.parametrize(
        "entry, message",
        [
            ({"version": "1.0.0", "moduleKey": "k"}, "no valid sha256"),
            ({"version": "1.0.0", "moduleKey": "k", "sha256": "ab"}, "no valid sha256"),
            ({"version": "1.0.0", "sha256": "a" * 64}, "no moduleKey"),
        ],
    )
    def test_an_entry_without_a_key_or_checksum_is_refused(self, entry, message):
        index = {"schemaVersion": 1, "versions": [entry]}
        with pytest.raises(IDPConfigurationError, match=message):
            tf.resolve_index_version(
                index, "1.0.0", "https://s3.us-west-2.amazonaws.com/b/p/i.json"
            )

    @pytest.mark.parametrize(
        "index",
        [
            {"versions": []},
            {"schemaVersion": 2, "versions": []},
            {"schemaVersion": 1, "versions": "1.0.0"},
            {"schemaVersion": 1, "versions": [{"sha256": "a"}]},
            [],
        ],
    )
    def test_a_malformed_or_newer_index_is_refused(self, index):
        with pytest.raises(IDPStackError, match="versions index"):
            tf.check_versions_index(index, "here")

    def test_a_version_with_a_module_url_is_refused(self):
        with pytest.raises(IDPConfigurationError, match="not both"):
            tf.resolve_module_source("https://x.test/m.zip", None, "1.0.0")

    def test_the_index_url_is_derived_from_the_variant_pointer(self):
        base = "https://s3.us-west-2.amazonaws.com/b/p/terraform"
        assert (
            tf.versions_index_url_for_pointer(
                f"{base}/idp-terraform-govcloud-latest.json"
            )
            == f"{base}/idp-terraform-govcloud-versions.json"
        )
        with pytest.raises(IDPConfigurationError):
            tf.versions_index_url_for_pointer(f"{base}/other.json")


# --------------------------------------------------------------------------------------
# Failures that are not races
# --------------------------------------------------------------------------------------


class TestFailures:
    def test_a_write_refused_for_another_reason_is_raised_not_retried(self, s3):
        from botocore.exceptions import ClientError

        puts = []

        def denied(**kw):
            puts.append(kw["Key"])
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject"
            )

        client = SimpleNamespace(
            meta=s3.meta, get_object=s3.get_object, put_object=denied
        )
        entry = tf.build_index_entry("1.0.0", "k", "0" * 64, "x")
        with pytest.raises(ClientError, match="AccessDenied"):
            tf.update_versions_index(client, bucket=BUCKET, key=INDEX_KEY, entry=entry)
        assert puts == [INDEX_KEY]

    def test_an_index_that_is_not_json_stops_the_publish(self, s3, tmp_path):
        s3.put_object(Bucket=BUCKET, Key=INDEX_KEY, Body=b"<html>")
        with pytest.raises(IDPStackError, match="is not JSON"):
            _publish(s3, tmp_path, "0.6.15")
        assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 1

    def test_an_index_that_is_not_json_stops_the_deploy(self):
        with pytest.raises(IDPConfigurationError, match="is not JSON"):
            tf.resolve_module_source(
                None,
                "https://s3.us-west-2.amazonaws.com/b/p/terraform/idp-terraform-latest.json",
                "1.0.0",
                fetch=lambda url: b"<html>",
            )

    def test_a_malformed_index_stops_the_deploy(self):
        with pytest.raises(IDPStackError, match="malformed"):
            tf.resolve_module_source(
                None,
                "https://s3.us-west-2.amazonaws.com/b/p/terraform/idp-terraform-latest.json",
                "1.0.0",
                fetch=lambda url: json.dumps({"schemaVersion": 1}).encode(),
            )


# --------------------------------------------------------------------------------------
# The release zip itself is immutable, whatever the index says
# --------------------------------------------------------------------------------------


def _release_objects(s3, version="0.6.15"):
    zip_key = tf.module_zip_key(PREFIX, version)
    out = {}
    for key in (zip_key, zip_key + ".sha256", INDEX_KEY, POINTER_KEY):
        try:
            out[key] = _read(s3, key)
        except s3.exceptions.NoSuchKey:
            out[key] = None
    return out


class TestReleaseZip:
    def test_a_concurrent_publish_of_the_same_release_cannot_replace_its_zip(
        self, s3, tmp_path
    ):
        """B publishes the release in full after A's index check and before A's
        zip write. A is refused at the zip, so every object is B's."""
        other = boto3.client("s3", region_name=REGION)
        zip_key = tf.module_zip_key(PREFIX, "0.6.15")
        b_dir = _module(tmp_path, "publisher B")
        fired = []

        def hook(params, **_):
            if params.get("Key") == zip_key and not fired:
                fired.append(1)
                tf.upload_module(
                    other,
                    module_dir=b_dir,
                    bucket=BUCKET,
                    prefix=PREFIX,
                    region=REGION,
                    version="0.6.15",
                    release_date="2026-11-20",
                )

        s3.meta.events.register("provide-client-params.s3.PutObject", hook)
        with pytest.raises(IDPStackError, match="already holds a module"):
            _publish(s3, tmp_path, "0.6.15", "publisher A")
        b_sha = hashlib.sha256(tf.deterministic_module_zip(b_dir)).hexdigest()
        objs = _release_objects(s3)
        assert hashlib.sha256(objs[zip_key]).hexdigest() == b_sha
        assert objs[zip_key + ".sha256"].decode().startswith(b_sha)
        assert [e["sha256"] for e in json.loads(objs[INDEX_KEY])["versions"]] == [b_sha]
        assert json.loads(objs[POINTER_KEY])["sha256"] == b_sha

    def test_a_release_missing_from_an_existing_index_is_not_replaced(
        self, s3, tmp_path
    ):
        """An SDK that kept no index published 0.6.15 after the index existed."""
        _publish(s3, tmp_path, "0.6.14", "old")
        _legacy_release(s3, "0.6.15", b"released")
        before = _release_objects(s3)
        with pytest.raises(IDPStackError, match="already holds a module"):
            _publish(s3, tmp_path, "0.6.15", "changed")
        assert _release_objects(s3) == before

    def test_a_release_zip_left_without_a_checksum_or_index_is_not_replaced(
        self, s3, tmp_path
    ):
        key = tf.module_zip_key(PREFIX, "0.6.15")
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"half-finished publish")
        with pytest.raises(IDPStackError, match="already holds a module"):
            _publish(s3, tmp_path, "0.6.15")
        assert _read(s3, key) == b"half-finished publish"
        assert _release_objects(s3)[INDEX_KEY] is None

    def test_the_same_release_bytes_republished_succeed(self, s3, tmp_path):
        """A re-run of a release publish that produced identical bytes, with an
        index entry missing (the earlier run stopped after the zip)."""
        d = _module(tmp_path, "same")
        data = tf.deterministic_module_zip(d)
        s3.put_object(Bucket=BUCKET, Key=tf.module_zip_key(PREFIX, "0.6.15"), Body=data)
        info = _publish(s3, tmp_path, "0.6.15", "same")
        assert info["sha256"] == hashlib.sha256(data).hexdigest()
        assert _versions(s3) == ["0.6.15"]

    def test_a_pre_release_zip_is_still_replaced(self, s3, tmp_path):
        key = tf.module_zip_key(PREFIX, "0.6.16.dev1")
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"older dev build")
        info = _publish(s3, tmp_path, "0.6.16.dev1", "newer")
        assert hashlib.sha256(_read(s3, key)).hexdigest() == info["sha256"]

    def test_an_old_botocore_is_refused_before_anything_is_written(self, s3, tmp_path):
        real = s3.meta.service_model

        class _Model:
            def operation_model(self, name):
                op = real.operation_model(name)
                shape = SimpleNamespace(
                    members={
                        k: v
                        for k, v in op.input_shape.members.items()
                        if k != "IfMatch"
                    }
                )
                return SimpleNamespace(input_shape=shape)

        puts = []
        old = SimpleNamespace(
            meta=SimpleNamespace(service_model=_Model()),
            get_object=s3.get_object,
            put_object=lambda **kw: puts.append(kw["Key"]),
            get_paginator=s3.get_paginator,
        )
        with pytest.raises(IDPConfigurationError, match="conditional writes"):
            tf.upload_module(
                old,
                module_dir=_module(tmp_path, "x"),
                bucket=BUCKET,
                prefix=PREFIX,
                region=REGION,
                version="0.6.15",
            )
        assert puts == []


# --------------------------------------------------------------------------------------
# The paths moto does not take on its own
# --------------------------------------------------------------------------------------


def _client_raising(s3, fail_codes, op="put_object", key=INDEX_KEY):
    """``s3`` with the first writes (or reads) of ``key`` failing with each of
    ``fail_codes`` in turn, then behaving normally."""
    from botocore.exceptions import ClientError

    codes = list(fail_codes)
    real = getattr(s3, op)

    def call(**kw):
        if kw.get("Key") == key and codes:
            code = codes.pop(0)
            raise ClientError({"Error": {"Code": code, "Message": code}}, "Op")
        return real(**kw)

    fake = SimpleNamespace(
        meta=s3.meta,
        get_object=s3.get_object,
        put_object=s3.put_object,
        get_paginator=s3.get_paginator,
    )
    setattr(fake, op, call)
    return fake


class TestRetryCodes:
    @pytest.mark.parametrize(
        "code", ["409", "ConditionalRequestConflict", "NoSuchKey", "412"]
    )
    def test_each_lost_race_code_is_retried(self, s3, code):
        client = _client_raising(s3, [code])
        entry = tf.build_index_entry("1.0.0", "k", "0" * 64, "x")
        tf.update_versions_index(client, bucket=BUCKET, key=INDEX_KEY, entry=entry)
        assert _versions(s3) == ["1.0.0"]

    def test_a_404_read_is_an_absent_index(self, s3):
        client = _client_raising(s3, ["404"], op="get_object")
        assert tf.read_versions_index(client, BUCKET, INDEX_KEY) == (None, None)

    def test_any_other_read_error_is_raised(self, s3):
        client = _client_raising(s3, ["AccessDenied"], op="get_object")
        with pytest.raises(IDPStackError, match="cannot read the Terraform versions"):
            tf.read_versions_index(client, BUCKET, INDEX_KEY)

    def test_a_conflicting_release_zip_write_is_retried(self, s3, tmp_path):
        zip_key = tf.module_zip_key(PREFIX, "0.6.15")
        client = _client_raising(s3, ["ConditionalRequestConflict"], key=zip_key)
        info = tf.upload_module(
            client,
            module_dir=_module(tmp_path, "x"),
            bucket=BUCKET,
            prefix=PREFIX,
            region=REGION,
            version="0.6.15",
        )
        assert hashlib.sha256(_read(s3, zip_key)).hexdigest() == info["sha256"]

    def test_a_release_zip_write_refused_for_another_reason_is_raised(
        self, s3, tmp_path
    ):
        from botocore.exceptions import ClientError

        zip_key = tf.module_zip_key(PREFIX, "0.6.15")
        client = _client_raising(s3, ["AccessDenied"], key=zip_key)
        with pytest.raises(ClientError, match="AccessDenied"):
            tf.upload_module(
                client,
                module_dir=_module(tmp_path, "x"),
                bucket=BUCKET,
                prefix=PREFIX,
                region=REGION,
                version="0.6.15",
            )
        assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 0


class TestPublicPath:
    """Releases publish with ``public=True``; the conditions must hold on both
    the ``public-read`` write and its retry without an ACL."""

    def test_a_race_on_the_public_read_write_is_retried(self, s3, tmp_path):
        _publish(s3, tmp_path, "0.6.14", "old", public=True)
        added = _interleave(s3)
        _publish(s3, tmp_path, "0.6.15", "new", public=True)
        assert added == ["9.9.0"]
        assert _versions(s3) == ["0.6.14", "0.6.15", "9.9.0"]

    def test_a_race_on_the_retry_without_an_acl_is_retried(self, s3, tmp_path):
        """A bucket that refuses ACLs: every public write is retried without
        one, and that retry must still carry the condition."""
        from botocore.exceptions import ClientError

        def no_acls(params, **_):
            if "ACL" in params:
                raise ClientError(
                    {"Error": {"Code": "AccessControlListNotSupported"}}, "PutObject"
                )

        s3.meta.events.register("provide-client-params.s3.PutObject", no_acls)
        _publish(s3, tmp_path, "0.6.14", "old", public=True)
        added = _interleave(s3)
        _publish(s3, tmp_path, "0.6.15", "new", public=True)
        assert added == ["9.9.0"]
        assert _versions(s3) == ["0.6.14", "0.6.15", "9.9.0"]


class TestBackfillEdges:
    def test_backfill_reads_every_listing_page(self, s3, tmp_path):
        pages = [
            {"CommonPrefixes": [{"Prefix": f"{PREFIX}/0.6.13/"}]},
            {"CommonPrefixes": [{"Prefix": f"{PREFIX}/0.6.14/"}]},
        ]
        _legacy_release(s3, "0.6.13", b"a")
        _legacy_release(s3, "0.6.14", b"b")
        client = SimpleNamespace(
            get_paginator=lambda name: SimpleNamespace(paginate=lambda **kw: pages),
            get_object=s3.get_object,
        )
        found = tf.discover_published_modules(client, BUCKET, PREFIX)
        assert [e["version"] for e in found] == ["0.6.13", "0.6.14"]

    def test_an_identical_republish_of_a_backfilled_release_keeps_one_entry(
        self, s3, tmp_path
    ):
        d = _module(tmp_path, "released")
        _legacy_release(s3, "0.6.15", tf.deterministic_module_zip(d))
        _publish(s3, tmp_path, "0.6.15", "released")
        assert _versions(s3) == ["0.6.15"]


class TestMessages:
    def test_a_trailing_newline_is_not_a_release_version(self):
        assert tf.is_release_version("0.6.15")
        assert not tf.is_release_version("0.6.15\n")
        assert not tf.is_release_version("0.6.15.dev1")

    def test_a_pinned_checksum_mismatch_names_the_index(self, monkeypatch):
        monkeypatch.setattr(tf, "fetch_url", lambda url, s3_factory=None: b"x")
        with pytest.raises(IDPConfigurationError, match="but the versions index says"):
            tf.load_module_manifest(
                "https://x.test/m.zip", "0" * 64, checksum_source="the versions index"
            )


# --------------------------------------------------------------------------------------
# Second review: the public path of the release zip, and its read after a 412
# --------------------------------------------------------------------------------------


def _is_public(s3, key):
    grants = s3.get_object_acl(Bucket=BUCKET, Key=key)["Grants"]
    return any(
        g["Grantee"].get("URI", "").endswith("/global/AllUsers")
        and g["Permission"] == "READ"
        for g in grants
    )


class TestReleaseZipPublic:
    def test_a_public_rerun_of_a_private_release_makes_every_object_public(
        self, s3, tmp_path
    ):
        """Identical bytes are not rewritten, so the ACL is applied instead."""
        _publish(s3, tmp_path, "0.6.15", "same")
        zip_key = tf.module_zip_key(PREFIX, "0.6.15")
        assert not _is_public(s3, zip_key)
        _publish(s3, tmp_path, "0.6.15", "same", public=True)
        for key in (zip_key, zip_key + ".sha256", INDEX_KEY, POINTER_KEY):
            assert _is_public(s3, key), key

    def test_a_bucket_refusing_acls_still_accepts_the_public_rerun(self, s3, tmp_path):
        from botocore.exceptions import ClientError

        _publish(s3, tmp_path, "0.6.15", "same")
        calls = []

        def no_acls(params, **_):
            calls.append(1)
            raise ClientError(
                {"Error": {"Code": "AccessControlListNotSupported"}}, "PutObjectAcl"
            )

        s3.meta.events.register("provide-client-params.s3.PutObjectAcl", no_acls)
        _publish(s3, tmp_path, "0.6.15", "same", public=True)
        assert calls  # the ACL was attempted, and its refusal tolerated

    def test_a_public_republish_with_other_bytes_does_not_replace_the_zip(
        self, s3, tmp_path
    ):
        zip_key = tf.module_zip_key(PREFIX, "0.6.15")
        s3.put_object(Bucket=BUCKET, Key=zip_key, Body=b"released")
        with pytest.raises(IDPStackError, match="already holds a module"):
            _publish(s3, tmp_path, "0.6.15", "changed", public=True)
        assert _read(s3, zip_key) == b"released"

    def test_a_public_republish_on_a_bucket_refusing_acls_does_not_replace_it(
        self, s3, tmp_path
    ):
        from botocore.exceptions import ClientError

        def no_acls(params, **_):
            if "ACL" in params:
                raise ClientError(
                    {"Error": {"Code": "AccessControlListNotSupported"}}, "PutObject"
                )

        s3.meta.events.register("provide-client-params.s3.PutObject", no_acls)
        zip_key = tf.module_zip_key(PREFIX, "0.6.15")
        s3.put_object(Bucket=BUCKET, Key=zip_key, Body=b"released")
        with pytest.raises(IDPStackError, match="already holds a module"):
            _publish(s3, tmp_path, "0.6.15", "changed", public=True)
        assert _read(s3, zip_key) == b"released"

    def test_a_zip_write_conflicting_every_time_fails_and_writes_nothing_else(
        self, s3, tmp_path
    ):
        zip_key = tf.module_zip_key(PREFIX, "0.6.15")
        client = _client_raising(
            s3, ["ConditionalRequestConflict"] * tf.VERSIONS_INDEX_ATTEMPTS, key=zip_key
        )
        with pytest.raises(IDPStackError, match="conflicted under every one"):
            tf.upload_module(
                client,
                module_dir=_module(tmp_path, "x"),
                bucket=BUCKET,
                prefix=PREFIX,
                region=REGION,
                version="0.6.15",
            )
        assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 0

    def test_a_zip_deleted_after_the_412_is_written_on_the_retry(self, s3, tmp_path):
        zip_key = tf.module_zip_key(PREFIX, "0.6.15")
        client = _client_raising(s3, ["PreconditionFailed"], key=zip_key)
        reads = _client_raising(s3, ["NoSuchKey"], op="get_object", key=zip_key)
        client.get_object = reads.get_object
        info = tf.upload_module(
            client,
            module_dir=_module(tmp_path, "x"),
            bucket=BUCKET,
            prefix=PREFIX,
            region=REGION,
            version="0.6.15",
        )
        assert hashlib.sha256(_read(s3, zip_key)).hexdigest() == info["sha256"]

    def test_any_other_read_failure_after_the_412_is_an_sdk_error(self, s3, tmp_path):
        zip_key = tf.module_zip_key(PREFIX, "0.6.15")
        client = _client_raising(s3, ["PreconditionFailed"], key=zip_key)
        reads = _client_raising(s3, ["AccessDenied"], op="get_object", key=zip_key)
        client.get_object = reads.get_object
        with pytest.raises(IDPStackError, match=f"cannot read s3://{BUCKET}/{zip_key}"):
            tf.upload_module(
                client,
                module_dir=_module(tmp_path, "x"),
                bucket=BUCKET,
                prefix=PREFIX,
                region=REGION,
                version="0.6.15",
            )
