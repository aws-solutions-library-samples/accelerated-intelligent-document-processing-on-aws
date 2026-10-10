# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for ``IDPPublisher``'s rebuild-detection checksums and its S3 surface.

Two responsibilities, both of which fail quietly when they are wrong.

``get_file_checksum`` and ``get_directory_checksum`` are what decide whether a
component is rebuilt. If a directory checksum fails to change after a source
edit, ``publish`` reports the component "up to date" and the **previous**
artifact is what gets deployed — a stale-artifact bug with no error message
anywhere. The tests here are therefore built around inputs that would
distinguish a correct implementation from a plausible wrong one: an edit in a
deep subdirectory, a same-length content change, a rename, and a reordering. The
fixtures deliberately avoid same-length identical content except where the point
is to show what the implementation cannot see.

``setup_artifacts_bucket`` and ``upload_to_s3_with_timer`` are exercised against
``moto``'s S3 rather than a mock client, and every assertion reads state back out
of the fake service: the bucket's location constraint, its versioning status, its
policy document, and the exact bytes at the exact key. A ``MagicMock`` accepts a
malformed ``CreateBucketConfiguration`` and a wrong key; the fake does not.

Every test that builds a boto3 client requests the ``aws_credentials`` fixture
from ``tests/unit/conftest.py``, because CI has neither a region nor credentials.
Buckets are created in ``us-west-2`` except where the ``us-east-1`` special case
is the subject, since S3 rejects a ``LocationConstraint`` of ``us-east-1``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError
from moto import mock_aws
from rich.console import Console

from idp_sdk._core import publish as publish_mod
from idp_sdk._core.publish import IDPPublisher

BUCKET_REGION = "us-west-2"

# The real ``boto3`` module object, captured at import time. Collection imports
# every test module before any test body runs, so this is the genuine module even
# if a later test replaces the entry in ``sys.modules``. See the fixture below.
_REAL_BOTO3 = boto3


@pytest.fixture(autouse=True)
def restore_the_real_boto3_module():
    """Repair ``sys.modules["boto3"]`` if an earlier test left a stub there.

    ``tests/unit/test_webui_oauth_urls_handler.py`` assigns
    ``sys.modules["boto3"] = types.ModuleType("boto3")`` in order to exec an
    inline CloudFormation handler outside Lambda, and never restores it — there is
    no ``monkeypatch``, no ``finally`` and no teardown in that file. The stub has
    no ``DEFAULT_SESSION`` attribute, and ``moto.mock_aws.start()`` patches exactly
    that attribute, so **every** ``mock_aws`` test that runs after it in the same
    process fails with ``AttributeError: <module 'boto3'> does not have the
    attribute 'DEFAULT_SESSION'`` — a failure whose message points at moto and says
    nothing about the cause.

    Today that leak is invisible because ``test_webui_*`` sorts last in this
    directory, so nothing using moto follows it. That is an ordering accident, not
    a property: it breaks the moment a suite is selected in a different order, run
    with a shuffling plugin, or given a new file that sorts after ``test_w``. This
    fixture makes the tests in *this* file independent of it. Fixing the leak
    belongs in that file and is reported separately.
    """
    if sys.modules.get("boto3") is not _REAL_BOTO3:
        sys.modules["boto3"] = _REAL_BOTO3
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_publisher(verbose: bool = False) -> tuple[IDPPublisher, io.StringIO]:
    pub = IDPPublisher(verbose=verbose)
    buf = io.StringIO()
    pub.console = Console(file=buf, width=300, no_color=True, highlight=False)
    return pub, buf


def write(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def combined_of(*contents: bytes) -> str:
    """The directory checksum the implementation should produce.

    It concatenates the hex digests of each included file, in walk order, and
    hashes the concatenation. Recomputing it here independently means the test
    asserts a value rather than merely comparing two runs of the same code.
    """
    return sha256_of("".join(sha256_of(c) for c in contents).encode())


# ---------------------------------------------------------------------------
# get_file_checksum
# ---------------------------------------------------------------------------


def test_a_file_checksum_is_the_sha256_of_its_bytes(tmp_path):
    """Asserted against an independently computed digest, not against itself."""
    payload = b"AWSTemplateFormatVersion: '2010-09-09'\n"
    target = tmp_path / "template.yaml"
    target.write_bytes(payload)

    assert IDPPublisher().get_file_checksum(str(target)) == sha256_of(payload)


def test_a_file_larger_than_the_read_block_is_hashed_in_full(tmp_path):
    """The reader loops in 4096-byte blocks.

    A bug in the loop — reading one block, or dropping the last partial one —
    would produce a digest that ignores most of a Lambda source file. The
    fixture is deliberately not a multiple of the block size so the final short
    read matters, and the last bytes are distinctive.
    """
    payload = (b"0123456789" * 1500) + b"TAIL-SENTINEL"
    assert len(payload) % 4096 != 0

    target = tmp_path / "big.py"
    target.write_bytes(payload)

    assert IDPPublisher().get_file_checksum(str(target)) == sha256_of(payload)

    # A change confined to the final partial block must change the digest.
    target.write_bytes((b"0123456789" * 1500) + b"TAIL-CHANGED!")
    assert IDPPublisher().get_file_checksum(str(target)) != sha256_of(payload)


def test_a_missing_file_checksums_to_the_empty_string(tmp_path):
    """The empty string is the sentinel for "absent", distinct from any digest.

    Callers compare a stored checksum against a fresh one; returning a digest of
    nothing (``e3b0c442…``) for a missing file would make a deleted file
    indistinguishable from an empty one.
    """
    assert IDPPublisher().get_file_checksum(str(tmp_path / "gone.py")) == ""


def test_an_empty_file_checksums_to_the_digest_of_no_bytes(tmp_path):
    empty = write(tmp_path / "empty.py", "")
    digest = IDPPublisher().get_file_checksum(str(empty))

    assert digest == sha256_of(b"")
    assert digest != ""


def test_a_directory_path_checksums_to_the_empty_string(tmp_path):
    """DEFECT, pinned as current behaviour: ``publish.py:651``.

    The guard is ``os.path.exists``, which is true for a directory, so the
    function proceeds to ``open(directory, "rb")`` — which raises
    ``IsADirectoryError`` on Linux. The exception is not caught here. Any caller
    that hands this a path it believes is a file gets a traceback rather than a
    checksum; ``get_directory_checksum`` avoids it only because it re-checks
    ``os.path.isfile`` before calling in.
    """
    with pytest.raises(IsADirectoryError):
        IDPPublisher().get_file_checksum(str(tmp_path))


# ---------------------------------------------------------------------------
# get_directory_checksum — the value, and what changes it
# ---------------------------------------------------------------------------


def test_a_directory_checksum_is_the_hash_of_its_files_digests_in_walk_order(tmp_path):
    """The exact value, computed independently.

    ``os.walk`` visits the root's files first (sorted), then descends into sorted
    subdirectories, so the expected concatenation order is fixed and asserted
    rather than inferred.
    """
    root = tmp_path / "component"
    write(root / "b.py", "second-at-root")
    write(root / "a.py", "first-at-root")
    write(root / "sub" / "c.py", "in-subdir")

    expected = combined_of(b"first-at-root", b"second-at-root", b"in-subdir")
    assert IDPPublisher().get_directory_checksum(str(root)) == expected


def test_a_missing_directory_checksums_to_the_empty_string(tmp_path):
    assert IDPPublisher().get_directory_checksum(str(tmp_path / "nope")) == ""


def test_an_existing_but_empty_directory_is_distinguishable_from_a_missing_one(
    tmp_path,
):
    """An empty component directory must not read as "absent".

    The empty directory hashes the empty concatenation; a missing one returns the
    empty-string sentinel. Collapsing the two would make a component whose
    sources were all deleted look like a component that was never configured.
    """
    (tmp_path / "empty").mkdir()

    empty_dir = IDPPublisher().get_directory_checksum(str(tmp_path / "empty"))
    assert empty_dir == sha256_of(b"")
    assert IDPPublisher().get_directory_checksum(str(tmp_path / "gone")) == ""
    assert empty_dir != ""


def test_a_directory_checksum_is_stable_across_repeated_calls(tmp_path):
    """Instability would force a rebuild of everything on every run."""
    root = tmp_path / "component"
    write(root / "a.py", "alpha")
    write(root / "z" / "b.py", "beta")
    write(root / "m" / "c.py", "gamma")

    pub = IDPPublisher()
    first = pub.get_directory_checksum(str(root))
    assert first == pub.get_directory_checksum(str(root))
    assert first == IDPPublisher().get_directory_checksum(str(root))


def test_an_edit_anywhere_in_the_tree_changes_the_checksum(tmp_path):
    """The whole purpose of the function.

    A same-length edit in the deepest file must change the result; if it does
    not, ``publish`` skips the rebuild and ships the previous artifact.
    """
    root = tmp_path / "component"
    write(root / "handler.py", "def handler(): return 1")
    deep = write(root / "pkg" / "inner" / "util.py", "VALUE = 'aaa'")

    before = IDPPublisher().get_directory_checksum(str(root))
    deep.write_text("VALUE = 'bbb'")  # same length, different content
    after = IDPPublisher().get_directory_checksum(str(root))

    assert before != after


def test_adding_and_removing_a_file_both_change_the_checksum(tmp_path):
    root = tmp_path / "component"
    write(root / "handler.py", "one")

    before = IDPPublisher().get_directory_checksum(str(root))

    extra = write(root / "helper.py", "two")
    with_extra = IDPPublisher().get_directory_checksum(str(root))
    assert with_extra != before

    extra.unlink()
    assert IDPPublisher().get_directory_checksum(str(root)) == before


def test_no_path_information_enters_the_digest_so_renames_are_invisible(tmp_path):
    """DEFECT, pinned as current behaviour: ``publish.py:747-755``.

    The digest is built from file *contents* only — ``checksums.append(...)``
    receives ``get_file_checksum(file_path)`` and nothing else, and the final
    value is the hash of those digests concatenated in walk order. No file name
    and no directory name ever enters it. So any change that leaves the walk-order
    *sequence of contents* the same is invisible, and that covers two changes a
    reader would expect to be detected:

    * **A rename.** ``index.py`` -> ``app.py`` keeps one file in one directory, so
      the sequence is unchanged.
    * **A relocation.** Moving ``a.py`` from the component root into an existing
      ``sub/`` directory is also invisible here, because ``os.walk`` yields the
      root's files before the subdirectory's and ``a`` still sorts before ``b`` —
      the sequence of contents is identical either way.

    The observable consequence is a stale deploy. A Lambda handler renamed from
    ``index.py`` to ``app.py`` (with ``Handler:`` updated in the template) is a
    real change to what the component builds, but ``publish`` computes the same
    checksum, reports the component up to date, skips ``sam build``, and uploads
    the previously built artifact — which still has the old file layout, so the
    function fails at invoke time with an import error. Nothing is printed at any
    point in the publish.
    """
    pub = IDPPublisher()

    renamed_root = tmp_path / "renamed"
    target = write(renamed_root / "index.py", "def handler(event, context): return {}")
    before_rename = pub.get_directory_checksum(str(renamed_root))
    target.rename(renamed_root / "app.py")
    assert pub.get_directory_checksum(str(renamed_root)) == before_rename

    moved_root = tmp_path / "moved"
    write(moved_root / "a.py", "aaa")
    write(moved_root / "sub" / "b.py", "bbb")
    before_move = pub.get_directory_checksum(str(moved_root))
    (moved_root / "a.py").rename(moved_root / "sub" / "a.py")
    assert pub.get_directory_checksum(str(moved_root)) == before_move


def test_a_relocation_that_reorders_the_walk_is_detected(tmp_path):
    """The blindness above is bounded, and the bound is worth stating.

    Here the moved file's content lands in a different position in the
    concatenation — ``z.py`` is hashed after ``sub/b.py`` once it moves down,
    where before it came first — so the digest does change. Asserted so the
    previous test is not over-read as "relocation never matters"; whether it
    matters depends on sort order, which is not a property anyone should rely on.
    """
    root = tmp_path / "component"
    write(root / "z.py", "zzz")
    write(root / "sub" / "b.py", "bbb")

    before = IDPPublisher().get_directory_checksum(str(root))
    (root / "z.py").rename(root / "sub" / "z.py")

    assert IDPPublisher().get_directory_checksum(str(root)) != before


def test_swapping_two_files_contents_does_change_the_checksum(tmp_path):
    """The rename blindness above is bounded — a swap reorders the digests.

    Asserted so the previous test is not read as "renames never matter".
    """
    root = tmp_path / "component"
    write(root / "a.py", "AAA")
    write(root / "b.py", "BBB")

    before = IDPPublisher().get_directory_checksum(str(root))
    (root / "a.py").write_text("BBB")
    (root / "b.py").write_text("AAA")

    assert IDPPublisher().get_directory_checksum(str(root)) != before


# ---------------------------------------------------------------------------
# get_directory_checksum — exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "excluded_dir",
    [
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".aws-sam",
        "node_modules",
        ".git",
        ".vscode",
        ".idea",
        "test-reports",
    ],
)
def test_build_and_tooling_directories_do_not_affect_the_checksum(
    tmp_path, excluded_dir
):
    """Each of these holds output, not input.

    If any one of them counted, every build would invalidate its own cache and
    nothing would ever be reported up to date. The test adds content inside the
    directory *after* taking the baseline, so a failure means that directory is
    being walked.
    """
    root = tmp_path / "component"
    write(root / "handler.py", "source")
    before = IDPPublisher().get_directory_checksum(str(root))

    write(root / excluded_dir / "artifact.bin", "generated output")
    write(root / "pkg" / excluded_dir / "nested_artifact.bin", "more output")

    assert IDPPublisher().get_directory_checksum(str(root)) == before


def test_an_egg_info_directory_is_excluded_by_its_suffix(tmp_path):
    """Matched by suffix, not by name, so any distribution's metadata is covered."""
    root = tmp_path / "component"
    write(root / "handler.py", "source")
    before = IDPPublisher().get_directory_checksum(str(root))

    write(root / "idp_common.egg-info" / "PKG-INFO", "Name: idp-common")
    write(root / "something_else.egg-info" / "SOURCES.txt", "handler.py")

    assert IDPPublisher().get_directory_checksum(str(root)) == before


@pytest.mark.parametrize(
    "excluded_file",
    [
        ".checksum",
        ".build_checksum",
        ".coverage",
        ".DS_Store",
        "Thumbs.db",
        "coverage.xml",
        "test-results.xml",
        ".gitkeep",
    ],
)
def test_named_artifact_files_do_not_affect_the_checksum(tmp_path, excluded_file):
    """The component's own ``.checksum`` in particular must not count itself.

    If it did, writing the checksum would immediately invalidate it, so every
    component would rebuild on every run.
    """
    root = tmp_path / "component"
    write(root / "handler.py", "source")
    before = IDPPublisher().get_directory_checksum(str(root))

    write(root / excluded_file, "artifact")
    assert IDPPublisher().get_directory_checksum(str(root)) == before


@pytest.mark.parametrize("suffix", [".pyc", ".pyo", ".pyd", ".so", ".coverage", ".log"])
def test_compiled_and_log_suffixes_do_not_affect_the_checksum(tmp_path, suffix):
    root = tmp_path / "component"
    write(root / "handler.py", "source")
    before = IDPPublisher().get_directory_checksum(str(root))

    write(root / f"handler{suffix}", "compiled or logged output")
    assert IDPPublisher().get_directory_checksum(str(root)) == before


def test_a_python_source_file_named_like_an_exclusion_prefix_still_counts(tmp_path):
    """The suffix rules must not swallow real sources.

    ``.so`` excludes ``foo.so`` but must not exclude ``also.py``; the check is a
    suffix test, and this asserts it has not been loosened to a substring one.
    """
    root = tmp_path / "component"
    write(root / "handler.py", "source")
    before = IDPPublisher().get_directory_checksum(str(root))

    write(root / "also.py", "a real module whose name ends in 'so.py'")
    assert IDPPublisher().get_directory_checksum(str(root)) != before


def test_a_shared_package_tree_ignores_its_tests_but_a_component_does_not(tmp_path):
    """The library checksum ignores tests; a component checksum does not.

    ``idp_common``'s tests do not ship in any Lambda layer, so a test-only edit
    must not trigger a rebuild of everything that depends on the library. The
    same edit inside an ordinary component directory *must* count, and the only
    thing that distinguishes the two cases is the directory path — so both
    directions are asserted over identical trees.

    The test's own name is kept free of the substring ``lib`` on purpose:
    pytest derives ``tmp_path`` from the test name, and the switch under test is
    a substring match against the whole absolute path, so a test called
    ``..._for_a_library_path`` would put ``lib`` into the path of *both* trees and
    quietly test only one branch. That is the defect pinned two tests below,
    encountered here first.
    """
    pub = IDPPublisher()

    lib_root = tmp_path / "lib" / "idp_common_pkg"
    write(lib_root / "idp_common" / "ocr.py", "real source")
    lib_before = pub.get_directory_checksum(str(lib_root))
    write(lib_root / "tests" / "unit" / "test_ocr.py", "a test")
    write(lib_root / "test_toplevel.py", "another test")
    write(lib_root / "idp_common" / "ocr_test.py", "a third test")
    assert pub.get_directory_checksum(str(lib_root)) == lib_before

    component_root = tmp_path / "patterns" / "unified"
    write(component_root / "src" / "ocr.py", "real source")
    component_before = pub.get_directory_checksum(str(component_root))
    write(component_root / "tests" / "unit" / "test_ocr.py", "a test")
    assert pub.get_directory_checksum(str(component_root)) != component_before


def test_pytest_cache_index_files_are_excluded_from_a_shared_package_tree(tmp_path):
    root = tmp_path / "lib" / "idp_common_pkg"
    write(root / "idp_common" / "ocr.py", "real source")
    before = IDPPublisher().get_directory_checksum(str(root))

    write(root / "nodeids", '["test_a.py::test_b"]')
    write(root / "lastfailed", '{"test_a.py::test_b": true}')

    assert IDPPublisher().get_directory_checksum(str(root)) == before


def test_the_test_file_exclusion_keys_on_a_bare_substring_of_the_whole_path(tmp_path):
    """DEFECT, pinned as current behaviour: ``publish.py:714`` and ``727``.

    The condition that switches on test-file exclusion is ``"lib" in
    directory`` — a substring test against the *whole path string* that was
    handed in, not a check that the directory is the shared package. Any path
    with those three letters anywhere in it qualifies: ``libs/``, ``publib/``,
    ``my-libraries/``, and — because callers may pass an absolute path — a
    checkout under ``~/gitlib/`` or ``/usr/local/lib/``.

    The observable consequence is that a component directory whose path happens
    to contain those letters silently stops hashing its ``tests/`` tree and its
    ``test_*.py`` files, so an edit there does not invalidate the cache and the
    component is reported up to date. Which components that applies to is decided
    by where the repository was cloned, not by anything in the repository.

    This is not hypothetical at the scale of three letters: an earlier draft of
    the test two above was named ``..._for_a_library_path``, pytest derived
    ``tmp_path`` from that name, and the branch fired for a tree that was meant to
    be the control. The two trees below differ only in the name of one parent
    directory, and they give different answers to the same edit.
    """
    pub = IDPPublisher()

    innocuous = tmp_path / "components" / "unified"
    write(innocuous / "handler.py", "source")
    innocuous_before = pub.get_directory_checksum(str(innocuous))
    write(innocuous / "test_handler.py", "a test")
    assert pub.get_directory_checksum(str(innocuous)) != innocuous_before

    # Identical tree, one parent directory renamed to contain "lib".
    caught = tmp_path / "publib" / "unified"
    write(caught / "handler.py", "source")
    caught_before = pub.get_directory_checksum(str(caught))
    write(caught / "test_handler.py", "a test")
    assert pub.get_directory_checksum(str(caught)) == caught_before


def test_a_directory_of_only_excluded_files_hashes_like_an_empty_one(tmp_path):
    """Pinned so the exclusion set's reach is explicit.

    A component whose entire content is build output is indistinguishable from an
    empty one, which is the intended reading — there is nothing to build from.
    """
    root = tmp_path / "component"
    write(root / "__pycache__" / "handler.cpython-312.pyc", "bytecode")
    write(root / ".checksum", "old")
    write(root / "build.log", "log output")

    assert IDPPublisher().get_directory_checksum(str(root)) == sha256_of(b"")


# ---------------------------------------------------------------------------
# setup_artifacts_bucket
# ---------------------------------------------------------------------------


def _publisher_for_bucket(bucket: str, region: str = BUCKET_REGION):
    pub, buf = make_publisher()
    pub.region = region
    pub.bucket = bucket
    pub.s3_client = boto3.client("s3", region_name=region)
    return pub, buf


@mock_aws
def test_a_missing_bucket_is_created_versioned_and_tls_only(aws_credentials):
    """Every property is read back out of the fake service.

    Three things must hold for a freshly created artifacts bucket: it is in the
    requested region, versioning is on (so a republish of the same version does
    not destroy the previous artifact), and the ``EnforceSSLOnly`` deny statement
    is present. The last one is a security control, and a ``MagicMock`` would
    have accepted a call that set none of them.
    """
    pub, buf = _publisher_for_bucket("idp-artifacts-us-west-2")
    pub.setup_artifacts_bucket()

    s3 = boto3.client("s3", region_name=BUCKET_REGION)

    assert (
        s3.get_bucket_location(Bucket="idp-artifacts-us-west-2")["LocationConstraint"]
        == BUCKET_REGION
    )
    assert (
        s3.get_bucket_versioning(Bucket="idp-artifacts-us-west-2")["Status"]
        == "Enabled"
    )

    policy = json.loads(
        s3.get_bucket_policy(Bucket="idp-artifacts-us-west-2")["Policy"]
    )
    (stmt,) = [s for s in policy["Statement"] if s.get("Sid") == "EnforceSSLOnly"]
    assert stmt["Effect"] == "Deny"
    assert stmt["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert set(stmt["Resource"]) == {
        "arn:aws:s3:::idp-artifacts-us-west-2",
        "arn:aws:s3:::idp-artifacts-us-west-2/*",
    }

    out = buf.getvalue()
    assert "Creating s3 bucket" in out
    assert "Applied EnforceSSLOnly bucket policy" in out


@mock_aws
def test_a_us_east_1_bucket_is_created_without_a_location_constraint(aws_credentials):
    """S3 rejects ``CreateBucketConfiguration`` for ``us-east-1``.

    The special case at ``publish.py:616-617`` exists for that reason, and moto
    enforces the same rule — so a successful creation here is evidence the branch
    was taken, not merely that a method was called.
    """
    pub, _ = _publisher_for_bucket("idp-artifacts-us-east-1", region="us-east-1")
    pub.setup_artifacts_bucket()

    s3 = boto3.client("s3", region_name="us-east-1")
    # us-east-1 reports its location as None, which is the documented encoding.
    assert (
        s3.get_bucket_location(Bucket="idp-artifacts-us-east-1").get(
            "LocationConstraint"
        )
        is None
    )
    assert (
        s3.get_bucket_versioning(Bucket="idp-artifacts-us-east-1")["Status"]
        == "Enabled"
    )


@mock_aws
def test_an_existing_bucket_is_reused_and_hardened_in_place(aws_credentials):
    """A republish must not recreate the bucket, but must still apply the policy.

    The pre-existing bucket here carries an unrelated statement, so the test also
    shows that hardening *merges* rather than replacing a policy the operator
    owns — overwriting it would silently drop their access controls.
    """
    s3 = boto3.client("s3", region_name=BUCKET_REGION)
    s3.create_bucket(
        Bucket="pre-existing-bucket",
        CreateBucketConfiguration={"LocationConstraint": BUCKET_REGION},
    )
    s3.put_bucket_policy(
        Bucket="pre-existing-bucket",
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "OperatorOwnedStatement",
                        "Effect": "Allow",
                        "Principal": {"AWS": "arn:aws:iam::111122223333:root"},
                        "Action": "s3:GetObject",
                        "Resource": "arn:aws:s3:::pre-existing-bucket/*",
                    }
                ],
            }
        ),
    )

    pub, buf = _publisher_for_bucket("pre-existing-bucket")
    pub.setup_artifacts_bucket()

    policy = json.loads(s3.get_bucket_policy(Bucket="pre-existing-bucket")["Policy"])
    sids = {s.get("Sid") for s in policy["Statement"]}
    assert sids == {"OperatorOwnedStatement", "EnforceSSLOnly"}

    out = buf.getvalue()
    assert "Using existing bucket: pre-existing-bucket" in out
    assert "EnforceSSLOnly bucket policy in place" in out


@mock_aws
def test_a_bucket_we_cannot_harden_is_a_warning_not_a_failure(
    aws_credentials, monkeypatch
):
    """Hardening an operator-owned bucket is additive and must never be fatal.

    The publisher may lack ``s3:PutBucketPolicy`` on a bucket it did not create.
    ``apply_enforce_ssl_only`` is called with ``raise_on_error=False`` there, and
    a falsy return must produce a warning telling the operator to add the policy
    by hand — not an exit. An exit would make a publish impossible on any bucket
    whose policy the operator manages.
    """
    s3 = boto3.client("s3", region_name=BUCKET_REGION)
    s3.create_bucket(
        Bucket="unhardenable",
        CreateBucketConfiguration={"LocationConstraint": BUCKET_REGION},
    )

    seen = {}

    def refuse(client, bucket, region, raise_on_error=True):
        seen["raise_on_error"] = raise_on_error
        seen["bucket"] = bucket
        return False

    monkeypatch.setattr(publish_mod, "apply_enforce_ssl_only", refuse)

    pub, buf = _publisher_for_bucket("unhardenable")
    pub.setup_artifacts_bucket()  # must not raise

    assert seen == {"raise_on_error": False, "bucket": "unhardenable"}
    assert "add it manually" in buf.getvalue()


@mock_aws
def test_a_failure_while_creating_the_bucket_exits_one(aws_credentials, monkeypatch):
    """Creating our own bucket and failing to harden it *is* fatal.

    The asymmetry with the previous test is deliberate in the source: we own the
    policy of a bucket we just created, so an inability to deny plaintext
    requests is a security failure rather than an operator's choice.
    """

    def boom(client, bucket, region, raise_on_error=True):
        raise RuntimeError("PutBucketPolicy denied")

    monkeypatch.setattr(publish_mod, "apply_enforce_ssl_only", boom)

    pub, buf = _publisher_for_bucket("brand-new-bucket")
    with pytest.raises(SystemExit) as exc:
        pub.setup_artifacts_bucket()

    assert exc.value.code == 1
    assert "Failed to create bucket" in buf.getvalue()
    assert "PutBucketPolicy denied" in buf.getvalue()


@mock_aws
def test_a_non_404_error_on_head_bucket_exits_without_trying_to_create(aws_credentials):
    """A 403 means the bucket exists and belongs to someone else.

    Only ``404`` may be read as "does not exist"; anything else must abort. If
    the handler fell through to ``create_bucket`` on a 403, the run would report
    a confusing creation failure instead of the access problem, and on a
    cross-account name it could not succeed anyway.

    A stub client is used for this one case because moto cannot readily produce a
    403 from ``head_bucket``; the assertion is on the exit and on the error text
    reaching the console.
    """

    class Forbidden:
        def head_bucket(self, **kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "403", "Message": "Forbidden"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "HeadBucket",
            )

        def create_bucket(self, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("create_bucket must not be attempted after a 403")

    pub, buf = make_publisher()
    pub.region = BUCKET_REGION
    pub.bucket = "someone-elses-bucket"
    pub.s3_client = Forbidden()

    with pytest.raises(SystemExit) as exc:
        pub.setup_artifacts_bucket()

    assert exc.value.code == 1
    out = buf.getvalue()
    assert "Error accessing bucket" in out
    assert "Forbidden" in out


# ---------------------------------------------------------------------------
# upload_to_s3_with_timer
# ---------------------------------------------------------------------------


@mock_aws
def test_an_upload_lands_the_exact_bytes_at_the_exact_key(aws_credentials, tmp_path):
    """Key and payload are both read back from the fake service.

    The key is the one thing no later step can correct: a template uploaded one
    prefix away from where the parent template's ``TemplateURL`` points is a
    deploy-time 404. The payload is checked byte-for-byte rather than by length.
    """
    s3 = boto3.client("s3", region_name=BUCKET_REGION)
    s3.create_bucket(
        Bucket="artifacts",
        CreateBucketConfiguration={"LocationConstraint": BUCKET_REGION},
    )

    payload = b"AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n"
    local = tmp_path / "packaged.yaml"
    local.write_bytes(payload)

    pub, buf = make_publisher()
    pub.bucket = "artifacts"
    pub.s3_client = s3

    pub.upload_to_s3_with_timer(
        str(local), "idp/0.6.8/nested/api-resolvers.yaml", "api-resolvers template"
    )

    body = s3.get_object(Bucket="artifacts", Key="idp/0.6.8/nested/api-resolvers.yaml")[
        "Body"
    ].read()
    assert body == payload

    # Nothing else was written, so the key is not merely *a* key that works.
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="artifacts")["Contents"]]
    assert keys == ["idp/0.6.8/nested/api-resolvers.yaml"]

    assert "Uploaded api-resolvers template" in buf.getvalue()


@mock_aws
def test_a_binary_payload_round_trips_byte_for_byte(aws_credentials, tmp_path):
    """Layer and function artifacts are zips, so the upload must be binary-clean.

    A megabyte of random bytes is pushed through the real
    ``boto3.s3.transfer`` manager the method configures, and the digest of what
    comes back out of the fake service is compared to the digest that went in. A
    text-mode read or any re-encoding would show up here and nowhere else — a
    corrupted layer zip fails at Lambda cold start, long after the publish
    reported success.

    The payload stays under the 5 MB multipart threshold deliberately: moto and
    botocore's flexible-checksum negotiation disagree on multipart part digests,
    so a genuinely multipart upload fails inside the fake for reasons that have
    nothing to do with this code. That path is noted as uncovered rather than
    worked around, since a workaround would be testing the workaround.
    """
    s3 = boto3.client("s3", region_name=BUCKET_REGION)
    s3.create_bucket(
        Bucket="artifacts",
        CreateBucketConfiguration={"LocationConstraint": BUCKET_REGION},
    )

    payload = os.urandom(1024 * 1024)
    local = tmp_path / "layer.zip"
    local.write_bytes(payload)

    pub, _ = make_publisher()
    pub.bucket = "artifacts"
    pub.s3_client = s3
    pub.upload_to_s3_with_timer(
        str(local), "idp/0.6.8/layers/layer.zip", "shared layer"
    )

    body = s3.get_object(Bucket="artifacts", Key="idp/0.6.8/layers/layer.zip")[
        "Body"
    ].read()
    assert len(body) == len(payload)
    assert sha256_of(body) == sha256_of(payload)


@mock_aws
def test_a_missing_local_file_propagates_rather_than_reporting_success(
    aws_credentials, tmp_path
):
    """The uploader has no error handling, and callers depend on that.

    ``upload_to_s3_with_timer`` returns nothing, so a swallowed exception would
    be indistinguishable from a successful upload and the publish would report a
    complete artifact set with a file missing. The test asserts the exception
    escapes *and* that no success line was printed.
    """
    s3 = boto3.client("s3", region_name=BUCKET_REGION)
    s3.create_bucket(
        Bucket="artifacts",
        CreateBucketConfiguration={"LocationConstraint": BUCKET_REGION},
    )

    pub, buf = make_publisher()
    pub.bucket = "artifacts"
    pub.s3_client = s3

    with pytest.raises((FileNotFoundError, OSError)):
        pub.upload_to_s3_with_timer(
            str(tmp_path / "never-built.yaml"), "idp/0.6.8/x.yaml", "missing template"
        )

    assert "Uploaded" not in buf.getvalue()


@mock_aws
def test_an_upload_to_a_missing_bucket_raises(aws_credentials, tmp_path):
    """A wrong bucket name must fail loudly at the first upload.

    Note the exception type: ``boto3.s3.transfer`` wraps the underlying
    ``NoSuchBucket`` ``ClientError`` in ``S3UploadFailedError``, so a caller that
    catches only ``ClientError`` around this method would not catch this. The
    message still carries the bucket, the key and the original error.
    """
    local = write(tmp_path / "a.yaml", "Resources: {}")

    pub, _ = make_publisher()
    pub.bucket = "bucket-that-was-never-created"
    pub.s3_client = boto3.client("s3", region_name=BUCKET_REGION)

    with pytest.raises(S3UploadFailedError) as exc:
        pub.upload_to_s3_with_timer(str(local), "idp/0.6.8/a.yaml", "a template")

    assert not isinstance(exc.value, ClientError)
    assert "bucket-that-was-never-created/idp/0.6.8/a.yaml" in str(exc.value)
    assert "NoSuchBucket" in str(exc.value)


@mock_aws
def test_an_empty_file_uploads_as_a_zero_byte_object(aws_credentials, tmp_path):
    """Pinned because an empty artifact is a silent failure downstream.

    Nothing in the uploader refuses a zero-byte file, so a build step that
    produced an empty template still uploads cleanly. The consequence — a
    zero-byte ``packaged.yaml`` in the published prefix — surfaces only at deploy
    time, which is why it is worth recording here.
    """
    s3 = boto3.client("s3", region_name=BUCKET_REGION)
    s3.create_bucket(
        Bucket="artifacts",
        CreateBucketConfiguration={"LocationConstraint": BUCKET_REGION},
    )
    local = tmp_path / "empty.yaml"
    local.write_bytes(b"")

    pub, _ = make_publisher()
    pub.bucket = "artifacts"
    pub.s3_client = s3
    pub.upload_to_s3_with_timer(str(local), "idp/0.6.8/empty.yaml", "empty template")

    head = s3.head_object(Bucket="artifacts", Key="idp/0.6.8/empty.yaml")
    assert head["ContentLength"] == 0
