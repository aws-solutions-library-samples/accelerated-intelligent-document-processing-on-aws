# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the upload side of ``idp_sdk._core.batch_processor``.

``BatchProcessor`` is how a batch of documents enters the accelerator. It
discovers the stack's buckets through ``StackInfo``, then for every document it
either uploads a local file, copies an object in from another bucket, or
validates that a key already sitting in the input bucket exists. The S3 key it
writes *is* the document id for the rest of the pipeline, and the object's
``config-version`` / ``config-revision`` user metadata is the only channel by
which a caller can pin which configuration processes the document — the queue
processor reads it off the object. A baseline tree may be copied alongside, for
automatic evaluation, and a metadata record of the whole batch is written to the
output bucket.

Two things shaped these tests.

**The assertions are made against a real fake, not a mock.** Every test here runs
inside ``moto.mock_aws`` with a real CloudFormation stack whose outputs
``StackInfo`` reads for itself, real S3 buckets, and a real DynamoDB table. The
code under test computes S3 keys and user metadata; a ``MagicMock`` accepts any
key and any metadata dict without complaint, so the tests upload and then read
back with ``head_object``/``get_object`` and assert on the key and the stored
attributes. Nothing here patches ``BatchProcessor`` or the S3 client except the
two places named in a docstring where an error condition cannot be provoked
through moto.

**Configuration pinning is checked on both paths that accept it.** A batch run
under the wrong configuration revision does not fail; it silently produces
results the caller then compares against something else. The manifest path and
the directory path both take ``config_revision``, so both are asserted, and they
do not agree — see ``test_a_directory_batch_silently_drops_the_pinned_config_revision``.
"""

import csv
import json
import os
import re

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_sdk._core.batch_processor import BatchProcessor

STACK_NAME = "test-idp-stack"
NO_BASELINE_STACK = "test-idp-stack-no-baseline"
INPUT_BUCKET = "idp-input-bucket"
OUTPUT_BUCKET = "idp-output-bucket"
BASELINE_BUCKET = "idp-baseline-bucket"
SOURCE_BUCKET = "someone-elses-bucket"
TRACKING_TABLE = "idp-tracking-table"

_STACK_OUTPUTS = {
    "S3InputBucketName": INPUT_BUCKET,
    "S3OutputBucketName": OUTPUT_BUCKET,
    "S3EvaluationBaselineBucketName": BASELINE_BUCKET,
    "LambdaLookupFunctionName": "idp-lookup-function",
}


def _stack_template(outputs, physical_suffix=""):
    """A template carrying the two resources and the outputs ``StackInfo`` reads.

    ``StackInfo.get_resources`` raises unless the stack has a ``DocumentQueue``
    logical resource, and it resolves the tracking table by listing stack
    resources, so both have to exist for the processor to construct at all.
    ``physical_suffix`` keeps two stacks in one account from claiming the same
    table and queue names.
    """
    return json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {
                "DocumentQueue": {
                    "Type": "AWS::SQS::Queue",
                    "Properties": {"QueueName": f"idp-document-queue{physical_suffix}"},
                },
                "TrackingTable": {
                    "Type": "AWS::DynamoDB::Table",
                    "Properties": {
                        "TableName": f"{TRACKING_TABLE}{physical_suffix}",
                        "KeySchema": [
                            {"AttributeName": "PK", "KeyType": "HASH"},
                            {"AttributeName": "SK", "KeyType": "RANGE"},
                        ],
                        "AttributeDefinitions": [
                            {"AttributeName": "PK", "AttributeType": "S"},
                            {"AttributeName": "SK", "AttributeType": "S"},
                        ],
                        "BillingMode": "PAY_PER_REQUEST",
                    },
                },
            },
            "Outputs": {key: {"Value": value} for key, value in outputs.items()},
        }
    )


@pytest.fixture
def idp_stack(aws_credentials):
    """A live moto account holding the stack, its buckets and its table.

    Yields the region, so a test that needs its own client builds one in the same
    place the processor does.
    """
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name=aws_credentials)
        cfn.create_stack(
            StackName=STACK_NAME, TemplateBody=_stack_template(_STACK_OUTPUTS)
        )
        # The same stack shape minus the baseline bucket output, for the branch
        # that skips baseline upload when the stack has no baseline bucket.
        without_baseline = {
            key: value
            for key, value in _STACK_OUTPUTS.items()
            if key != "S3EvaluationBaselineBucketName"
        }
        cfn.create_stack(
            StackName=NO_BASELINE_STACK,
            TemplateBody=_stack_template(without_baseline, physical_suffix="-nb"),
        )

        s3 = boto3.client("s3", region_name=aws_credentials)
        for bucket in (INPUT_BUCKET, OUTPUT_BUCKET, BASELINE_BUCKET, SOURCE_BUCKET):
            s3.create_bucket(Bucket=bucket)
        yield aws_credentials


@pytest.fixture
def processor(idp_stack):
    """A ``BatchProcessor`` built the way production builds it: from the stack."""
    return BatchProcessor(stack_name=STACK_NAME, region=idp_stack)


@pytest.fixture
def s3(idp_stack):
    """An independent S3 client, for arranging inputs and reading results back."""
    return boto3.client("s3", region_name=idp_stack)


def _write_pdf(directory, name, body=b"%PDF-1.4 fake"):
    """Create a file under ``directory`` (creating parents) and return its path."""
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return str(path)


def _write_manifest(tmp_path, rows, name="manifest.csv"):
    """Write a CSV manifest with the columns the parser understands."""
    path = tmp_path / name
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["document_path", "baseline_source"])
        writer.writeheader()
        for row in rows:
            writer.writerow({"baseline_source": "", **row})
    return str(path)


def _keys(s3_client, bucket, prefix=""):
    """Every key in ``bucket`` under ``prefix``, sorted."""
    response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return sorted(obj["Key"] for obj in response.get("Contents", []))


@pytest.mark.unit
@pytest.mark.batch
class TestConstruction:
    """What the processor learns about the stack before it does anything."""

    def test_resource_names_are_read_from_the_stack_outputs(self, processor):
        assert processor.resources["InputBucket"] == INPUT_BUCKET
        assert processor.resources["OutputBucket"] == OUTPUT_BUCKET
        assert processor.resources["EvaluationBaselineBucket"] == BASELINE_BUCKET
        # Resolved by listing stack resources rather than from an output.
        assert processor.resources["DocumentsTable"] == TRACKING_TABLE

    def test_a_stack_that_does_not_exist_is_refused(self, idp_stack):
        """Construction must fail loudly rather than upload into an empty name.

        ``InputBucket`` would otherwise resolve to ``""`` and every upload would
        fail one document at a time, counted as a per-document failure.
        """
        with pytest.raises(ValueError, match="not in a valid state"):
            BatchProcessor(stack_name="no-such-stack", region=idp_stack)


@pytest.mark.unit
@pytest.mark.batch
class TestBatchIdGeneration:
    """The batch id is the S3 key prefix, so its shape is a contract."""

    def test_the_generated_id_is_the_prefix_and_a_utc_timestamp(self, processor):
        batch_id = processor._generate_batch_id("experiment-v1")
        assert re.fullmatch(r"experiment-v1-\d{8}-\d{6}", batch_id), batch_id

    def test_uploaded_keys_sit_under_the_generated_id(self, processor, s3, tmp_path):
        _write_pdf(tmp_path, "a.pdf")
        result = processor.process_batch_from_directory(str(tmp_path))
        batch_id = result["batch_id"]
        assert batch_id.startswith("cli-batch-")
        assert _keys(s3, INPUT_BUCKET) == [f"{batch_id}/a.pdf"]


@pytest.mark.unit
@pytest.mark.batch
class TestProcessBatchRouting:
    """``process_batch`` is the one entry point the CLI and SDK both call."""

    def test_no_source_at_all_is_refused(self, processor):
        with pytest.raises(ValueError, match="Must specify one of"):
            processor.process_batch()

    def test_a_manifest_routes_to_the_manifest_path(self, processor, s3, tmp_path):
        local = _write_pdf(tmp_path, "docs/invoice.pdf")
        manifest = _write_manifest(tmp_path, [{"document_path": local}])

        result = processor.process_batch(manifest_path=manifest)

        # A manifest document has no relative path, so the key is id/filename —
        # the directory it came from is deliberately not preserved.
        assert _keys(s3, INPUT_BUCKET) == [f"{result['batch_id']}/invoice.pdf"]
        assert result["source"] == manifest

    def test_a_directory_routes_to_the_directory_scan(self, processor, s3, tmp_path):
        _write_pdf(tmp_path, "nested/deep/report.pdf")

        result = processor.process_batch(directory=str(tmp_path))

        # The directory path preserves structure below the scanned root.
        assert _keys(s3, INPUT_BUCKET) == [
            f"{result['batch_id']}/nested/deep/report.pdf"
        ]

    def test_an_s3_uri_routes_to_the_copy_path(self, processor, s3, tmp_path):
        s3.put_object(Bucket=SOURCE_BUCKET, Key="incoming/scan.pdf", Body=b"from-s3")

        result = processor.process_batch(s3_uri=f"s3://{SOURCE_BUCKET}/incoming/")

        key = f"{result['batch_id']}/scan.pdf"
        assert _keys(s3, INPUT_BUCKET) == [key]
        assert s3.get_object(Bucket=INPUT_BUCKET, Key=key)["Body"].read() == b"from-s3"

    def test_batch_prefix_overrides_output_prefix(self, processor, s3, tmp_path):
        """The CLI spells the prefix ``batch_prefix``; it must win, not be ignored."""
        _write_pdf(tmp_path, "a.pdf")

        result = processor.process_batch(
            directory=str(tmp_path), output_prefix="ignored", batch_prefix="from-cli"
        )

        assert result["batch_id"].startswith("from-cli-")
        assert result["output_prefix"] == "from-cli"

    def test_config_path_is_retained_on_the_processor(self, processor, tmp_path):
        _write_pdf(tmp_path, "a.pdf")
        config = str(tmp_path / "config.yaml")

        processor.process_batch(directory=str(tmp_path), config_path=config)

        assert processor.config_path == config

    def test_number_of_files_caps_a_manifest_batch(self, processor, s3, tmp_path):
        """The cap is applied after parsing, so a long manifest can be smoke-run
        over its first few documents without being edited."""
        rows = [
            {"document_path": _write_pdf(tmp_path, f"doc{index}.pdf")}
            for index in range(4)
        ]
        manifest = _write_manifest(tmp_path, rows)

        result = processor.process_batch(
            manifest_path=manifest, batch_id="b", number_of_files=2
        )

        assert result["queued"] == 2
        assert _keys(s3, INPUT_BUCKET) == ["b/doc0.pdf", "b/doc1.pdf"]

    def test_an_explicit_batch_id_is_used_verbatim(self, processor, s3, tmp_path):
        _write_pdf(tmp_path, "a.pdf")

        result = processor.process_batch(directory=str(tmp_path), batch_id="rerun-7")

        assert result["batch_id"] == "rerun-7"
        assert _keys(s3, INPUT_BUCKET) == ["rerun-7/a.pdf"]


@pytest.mark.unit
@pytest.mark.batch
class TestDirectoryScan:
    """Which local files become documents, and what key each one gets."""

    def test_recursive_scan_includes_the_root_and_subdirectories(
        self, processor, s3, tmp_path
    ):
        _write_pdf(tmp_path, "top.pdf")
        _write_pdf(tmp_path, "sub/mid.pdf")
        _write_pdf(tmp_path, "sub/deeper/low.pdf")

        result = processor.process_batch_from_directory(
            str(tmp_path), batch_id="b", recursive=True
        )

        assert _keys(s3, INPUT_BUCKET) == [
            "b/sub/deeper/low.pdf",
            "b/sub/mid.pdf",
            "b/top.pdf",
        ]
        assert result["uploaded"] == 3
        assert result["queued"] == 3

    def test_a_non_recursive_scan_stops_at_the_root(self, processor, s3, tmp_path):
        _write_pdf(tmp_path, "top.pdf")
        _write_pdf(tmp_path, "sub/mid.pdf")

        processor.process_batch_from_directory(
            str(tmp_path), batch_id="b", recursive=False
        )

        assert _keys(s3, INPUT_BUCKET) == ["b/top.pdf"]

    def test_the_file_pattern_selects_which_files_are_documents(
        self, processor, s3, tmp_path
    ):
        _write_pdf(tmp_path, "keep.pdf")
        _write_pdf(tmp_path, "skip.txt")

        processor.process_batch_from_directory(
            str(tmp_path), file_pattern="*.pdf", batch_id="b"
        )

        assert _keys(s3, INPUT_BUCKET) == ["b/keep.pdf"]

    def test_a_directory_matching_nothing_is_an_error_not_an_empty_batch(
        self, processor, tmp_path
    ):
        """An empty batch would report success and queue nothing at all."""
        _write_pdf(tmp_path, "only.txt")

        with pytest.raises(ValueError, match="No documents found matching"):
            processor.process_batch_from_directory(str(tmp_path), file_pattern="*.pdf")

    def test_number_of_files_caps_the_batch(self, processor, s3, tmp_path):
        for index in range(5):
            _write_pdf(tmp_path, f"doc{index}.pdf")

        result = processor.process_batch_from_directory(
            str(tmp_path), batch_id="b", number_of_files=2
        )

        assert len(_keys(s3, INPUT_BUCKET)) == 2
        assert result["queued"] == 2

    def test_a_scan_records_the_relative_path_and_the_document_id(
        self, processor, tmp_path
    ):
        _write_pdf(tmp_path, "sub/a.pdf")

        documents = processor._scan_local_directory(str(tmp_path), "*.pdf", True)

        assert documents == [
            {
                "document_id": "sub/a",
                "path": os.path.join(str(tmp_path), "sub", "a.pdf"),
                "filename": "a.pdf",
                "relative_path": os.path.join("sub", "a.pdf"),
                "type": "local",
            }
        ]


@pytest.mark.unit
@pytest.mark.batch
class TestLocalUploadAndConfigPinning:
    """The bytes and the user metadata that reach the input bucket."""

    def test_the_uploaded_object_holds_the_local_bytes(self, processor, s3, tmp_path):
        _write_pdf(tmp_path, "a.pdf", body=b"exact-bytes-123")

        processor.process_batch_from_directory(str(tmp_path), batch_id="b")

        body = s3.get_object(Bucket=INPUT_BUCKET, Key="b/a.pdf")["Body"].read()
        assert body == b"exact-bytes-123"

    def test_without_a_config_version_no_metadata_is_stored(
        self, processor, s3, tmp_path
    ):
        _write_pdf(tmp_path, "a.pdf")

        processor.process_batch_from_directory(str(tmp_path), batch_id="b")

        assert s3.head_object(Bucket=INPUT_BUCKET, Key="b/a.pdf")["Metadata"] == {}

    def test_a_config_version_is_stored_as_object_metadata(
        self, processor, s3, tmp_path
    ):
        """This metadata is the whole pinning mechanism: the queue processor
        reads ``config-version`` off the object to decide which configuration
        profile processes the document."""
        _write_pdf(tmp_path, "a.pdf", body=b"body")

        processor.process_batch_from_directory(
            str(tmp_path), batch_id="b", config_version="tuned-profile"
        )

        head = s3.head_object(Bucket=INPUT_BUCKET, Key="b/a.pdf")
        assert head["Metadata"] == {"config-version": "tuned-profile"}
        # put_object is used on this path; the bytes must still be the file's.
        assert (
            s3.get_object(Bucket=INPUT_BUCKET, Key="b/a.pdf")["Body"].read() == b"body"
        )

    def test_a_revision_is_stamped_alongside_a_profile(self, processor, s3, tmp_path):
        local = _write_pdf(tmp_path, "a.pdf")

        processor._upload_local_file_with_path(
            {"path": local, "filename": "a.pdf", "type": "local"},
            batch_id="b",
            config_version="tuned-profile",
            config_revision=7,
        )

        assert s3.head_object(Bucket=INPUT_BUCKET, Key="b/a.pdf")["Metadata"] == {
            "config-version": "tuned-profile",
            "config-revision": "7",
        }

    def test_a_revision_alone_is_not_stamped_without_a_profile(
        self, processor, s3, tmp_path
    ):
        """A revision number means nothing without the profile it belongs to, so
        the no-profile branch stores no metadata at all."""
        local = _write_pdf(tmp_path, "a.pdf")

        processor._upload_local_file_with_path(
            {"path": local, "filename": "a.pdf", "type": "local"},
            batch_id="b",
            config_revision=7,
        )

        assert s3.head_object(Bucket=INPUT_BUCKET, Key="b/a.pdf")["Metadata"] == {}

    def test_a_manifest_batch_stamps_both_profile_and_revision(
        self, processor, s3, tmp_path
    ):
        local = _write_pdf(tmp_path, "a.pdf")
        manifest = _write_manifest(tmp_path, [{"document_path": local}])

        processor.process_batch(
            manifest_path=manifest,
            batch_id="b",
            config_version="tuned-profile",
            config_revision=7,
        )

        assert s3.head_object(Bucket=INPUT_BUCKET, Key="b/a.pdf")["Metadata"] == {
            "config-version": "tuned-profile",
            "config-revision": "7",
        }

    def test_a_directory_batch_silently_drops_the_pinned_config_revision(
        self, processor, s3, tmp_path
    ):
        """DEFECT, pinned as-is: ``batch_processor.py:242``.

        ``process_batch_from_directory`` accepts ``config_revision`` and forwards
        its arguments to ``_process_documents`` positionally — ``documents,
        batch_id, output_prefix, dir_path, config_version, base_dir=dir_path`` —
        so ``config_revision`` is never passed on and defaults to ``None``. The
        uploaded object therefore carries ``config-version`` but no
        ``config-revision``, and the queue processor pins the profile's *current*
        revision instead of the one the caller asked for.

        The consequence is a silently wrong answer rather than a failure: the run
        succeeds, and the caller believes they scored revision 7 while the work
        ran under whatever the profile holds now. The manifest path, asserted
        directly above, forwards it correctly, which is what makes this a defect
        and not a design decision.

        When it is fixed, this test fails and the expected metadata becomes the
        two-key dict the manifest test asserts.
        """
        _write_pdf(tmp_path, "a.pdf")

        processor.process_batch_from_directory(
            str(tmp_path),
            batch_id="b",
            config_version="tuned-profile",
            config_revision=7,
        )

        assert s3.head_object(Bucket=INPUT_BUCKET, Key="b/a.pdf")["Metadata"] == {
            "config-version": "tuned-profile"
        }


@pytest.mark.unit
@pytest.mark.batch
class TestS3SourceScanAndCopy:
    """Documents that start life in another bucket."""

    def test_an_s3_uri_must_name_a_scheme(self, processor):
        with pytest.raises(ValueError, match="Must start with s3://"):
            processor.process_batch_from_s3_uri("/local/path")

    def test_a_prefix_matching_nothing_is_an_error(self, processor, s3):
        s3.put_object(Bucket=SOURCE_BUCKET, Key="empty/notes.txt", Body=b"x")

        with pytest.raises(ValueError, match="No documents found matching"):
            processor.process_batch_from_s3_uri(f"s3://{SOURCE_BUCKET}/empty/")

    def test_the_scan_skips_directory_markers_and_applies_the_pattern(
        self, processor, s3
    ):
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/", Body=b"")
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/a.pdf", Body=b"a")
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/notes.txt", Body=b"n")

        documents = processor._scan_s3_uri(SOURCE_BUCKET, "in", "*.pdf", True)

        assert documents == [
            {
                "document_id": "a",
                "path": f"s3://{SOURCE_BUCKET}/in/a.pdf",
                "filename": "a.pdf",
                "type": "s3",
            }
        ]

    def test_a_non_recursive_scan_excludes_nested_keys(self, processor, s3):
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/a.pdf", Body=b"a")
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/more/b.pdf", Body=b"b")

        documents = processor._scan_s3_uri(SOURCE_BUCKET, "in/", "*.pdf", False)

        assert [doc["filename"] for doc in documents] == ["a.pdf"]

    def test_a_recursive_scan_flattens_nested_keys_onto_one_prefix(self, processor, s3):
        """Copied documents lose their source directory structure: the key is
        ``batch_id/filename``, so two same-named files under different prefixes
        collide on one key. Pinned because the directory path behaves the other
        way and the difference is easy to assume away."""
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/one/a.pdf", Body=b"first")
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/two/a.pdf", Body=b"second")

        result = processor.process_batch_from_s3_uri(
            f"s3://{SOURCE_BUCKET}/in/", batch_id="b"
        )

        assert result["queued"] == 2
        assert _keys(s3, INPUT_BUCKET) == ["b/a.pdf"]

    def test_a_copied_document_is_not_counted_as_an_upload(self, processor, s3):
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/a.pdf", Body=b"a")

        result = processor.process_batch_from_s3_uri(
            f"s3://{SOURCE_BUCKET}/in/", batch_id="b"
        )

        assert result["queued"] == 1
        assert result["uploaded"] == 0

    def test_a_scan_of_a_bucket_that_cannot_be_listed_is_raised_not_swallowed(
        self, processor, caplog
    ):
        """An unreadable source must not read as "no documents here", which is
        the other plausible outcome and would report a successful empty batch."""
        from botocore.exceptions import ClientError as BotoClientError

        with caplog.at_level("ERROR"):
            with pytest.raises(BotoClientError):
                processor._scan_s3_uri("no-such-bucket", "in/", "*.pdf", True)

        assert "Error scanning S3 URI" in caplog.text

    def test_a_bucket_only_uri_has_no_key_to_copy(self, processor):
        with pytest.raises(ValueError, match=r"Invalid S3 URI \(no key\)"):
            processor._copy_s3_file(
                {"path": f"s3://{SOURCE_BUCKET}", "filename": "a.pdf", "type": "s3"},
                batch_id="b",
            )

    def test_a_copy_replaces_metadata_when_a_profile_is_pinned(self, processor, s3):
        """``MetadataDirective=REPLACE`` is required for the new metadata to take
        effect; without it S3 keeps the source object's metadata and the pin is
        lost on this path."""
        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key="in/a.pdf",
            Body=b"a",
            Metadata={"config-version": "stale-profile"},
        )

        dest_key = processor._copy_s3_file(
            {"path": f"s3://{SOURCE_BUCKET}/in/a.pdf", "filename": "a.pdf"},
            batch_id="b",
            config_version="fresh-profile",
            config_revision=3,
        )

        assert dest_key == "b/a.pdf"
        assert s3.head_object(Bucket=INPUT_BUCKET, Key=dest_key)["Metadata"] == {
            "config-version": "fresh-profile",
            "config-revision": "3",
        }

    def test_a_copy_without_a_profile_keeps_the_source_metadata(self, processor, s3):
        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key="in/a.pdf",
            Body=b"a",
            Metadata={"origin": "upstream"},
        )

        processor._copy_s3_file(
            {"path": f"s3://{SOURCE_BUCKET}/in/a.pdf", "filename": "a.pdf"},
            batch_id="b",
        )

        assert s3.head_object(Bucket=INPUT_BUCKET, Key="b/a.pdf")["Metadata"] == {
            "origin": "upstream"
        }


@pytest.mark.unit
@pytest.mark.batch
class TestExistingKeyReference:
    """``s3-key`` documents are already in the input bucket; only validated."""

    def test_an_existing_key_is_referenced_without_being_rewritten(self, processor, s3):
        s3.put_object(Bucket=INPUT_BUCKET, Key="already/there.pdf", Body=b"original")

        key = processor._process_document_with_base(
            {"path": "already/there.pdf", "filename": "there.pdf", "type": "s3-key"},
            batch_id="b",
        )

        assert key == "already/there.pdf"
        assert _keys(s3, INPUT_BUCKET) == ["already/there.pdf"]
        body = s3.get_object(Bucket=INPUT_BUCKET, Key=key)["Body"].read()
        assert body == b"original"

    def test_a_missing_key_is_reported_as_a_missing_document(self, processor):
        with pytest.raises(ValueError, match="Document not found in InputBucket"):
            processor._validate_s3_key("nothing/here.pdf")

    def test_an_error_other_than_absence_is_not_reported_as_absence(
        self, processor, monkeypatch
    ):
        """A permission failure must not be translated into "not found".

        moto answers every reachable ``head_object`` with either success or 404,
        so the non-404 ``ClientError`` branch is provoked by replacing the one
        client call; the assertion is still on which exception escapes.
        """

        def deny(**kwargs):
            raise ClientError(
                {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
            )

        monkeypatch.setattr(processor.s3, "head_object", deny)

        with pytest.raises(ClientError):
            processor._validate_s3_key("any/key.pdf")

    def test_an_unknown_document_type_is_refused(self, processor):
        with pytest.raises(ValueError, match="Unknown document type: ftp"):
            processor._process_document_with_base(
                {"path": "x", "filename": "x", "type": "ftp"}, batch_id="b"
            )


@pytest.mark.unit
@pytest.mark.batch
class TestBaselineUpload:
    """Baseline trees travel beside the document, for automatic evaluation."""

    def test_a_local_baseline_tree_is_uploaded_under_the_document_key(
        self, processor, s3, tmp_path
    ):
        """The baseline key must mirror the document key exactly, or the
        evaluation step looks for the baseline where it is not."""
        local = _write_pdf(tmp_path / "docs", "a.pdf")
        baseline = tmp_path / "baseline"
        _write_pdf(baseline, "sections/1/result.json", body=b"{}")
        _write_pdf(baseline, "summary.json", body=b"{}")
        manifest = _write_manifest(
            tmp_path,
            [{"document_path": local, "baseline_source": str(baseline)}],
        )

        result = processor.process_batch(manifest_path=manifest, batch_id="b")

        assert result["baselines_uploaded"] == 1
        assert _keys(s3, BASELINE_BUCKET) == [
            "b/a.pdf/sections/1/result.json",
            "b/a.pdf/summary.json",
        ]

    def test_a_directory_baseline_key_follows_the_preserved_path(
        self, processor, s3, tmp_path
    ):
        baseline = tmp_path / "baseline"
        _write_pdf(baseline, "result.json", body=b"{}")

        processor._upload_baseline(
            {
                "filename": "a.pdf",
                "relative_path": os.path.join("sub", "a.pdf"),
                "baseline_source": str(baseline),
            },
            batch_id="b",
        )

        assert _keys(s3, BASELINE_BUCKET) == ["b/sub/a.pdf/result.json"]

    def test_an_s3_baseline_tree_is_copied_with_its_structure(
        self, processor, s3, tmp_path
    ):
        s3.put_object(Bucket=SOURCE_BUCKET, Key="base/a.pdf/pages/1.json", Body=b"p")
        s3.put_object(Bucket=SOURCE_BUCKET, Key="base/a.pdf/summary.json", Body=b"s")

        processor._upload_baseline(
            {
                "filename": "a.pdf",
                "baseline_source": f"s3://{SOURCE_BUCKET}/base/a.pdf",
            },
            batch_id="b",
        )

        assert _keys(s3, BASELINE_BUCKET) == [
            "b/a.pdf/pages/1.json",
            "b/a.pdf/summary.json",
        ]
        body = s3.get_object(Bucket=BASELINE_BUCKET, Key="b/a.pdf/summary.json")
        assert body["Body"].read() == b"s"

    def test_a_document_with_no_baseline_source_uploads_nothing(self, processor, s3):
        processor._upload_baseline({"filename": "a.pdf"}, batch_id="b")

        assert _keys(s3, BASELINE_BUCKET) == []

    def test_a_stack_without_a_baseline_bucket_skips_the_upload(
        self, idp_stack, s3, tmp_path
    ):
        """A stack deployed without evaluation must still process documents."""
        baseline = tmp_path / "baseline"
        _write_pdf(baseline, "result.json", body=b"{}")
        no_baseline = BatchProcessor(stack_name=NO_BASELINE_STACK, region=idp_stack)

        no_baseline._upload_baseline(
            {"filename": "a.pdf", "baseline_source": str(baseline)}, batch_id="b"
        )

        assert _keys(s3, BASELINE_BUCKET) == []

    def test_a_missing_local_baseline_directory_is_refused(self, processor, tmp_path):
        with pytest.raises(ValueError, match="Baseline directory not found"):
            processor._upload_local_baseline_tree(
                str(tmp_path / "absent"), BASELINE_BUCKET, "b/a.pdf"
            )

    def test_a_failed_baseline_does_not_stop_the_document(
        self, processor, s3, tmp_path
    ):
        """The document is the payload; a baseline is an evaluation convenience.
        Losing the document because its baseline was mistyped would be the
        expensive failure, so the baseline error is logged and swallowed."""
        local = _write_pdf(tmp_path / "docs", "a.pdf")
        manifest = _write_manifest(
            tmp_path,
            [{"document_path": local, "baseline_source": str(tmp_path / "absent")}],
        )

        result = processor.process_batch(manifest_path=manifest, batch_id="b")

        assert result["baselines_uploaded"] == 0
        assert result["queued"] == 1
        assert _keys(s3, INPUT_BUCKET) == ["b/a.pdf"]


@pytest.mark.unit
@pytest.mark.batch
class TestResultAccountingAndMetadata:
    """What the caller is told, and what is left behind in the output bucket."""

    def test_a_failing_document_is_counted_without_losing_the_others(
        self, processor, s3
    ):
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/good.pdf", Body=b"g")
        documents = [
            {
                "document_id": "good",
                "path": f"s3://{SOURCE_BUCKET}/in/good.pdf",
                "filename": "good.pdf",
                "type": "s3",
            },
            {
                "document_id": "bad",
                "path": "missing/bad.pdf",
                "filename": "bad.pdf",
                "type": "s3-key",
            },
        ]

        result = processor._process_documents(documents, "b", "cli-batch", "inline")

        assert result["queued"] == 1
        assert result["failed"] == 1
        assert result["document_ids"] == ["b/good.pdf"]

    def test_a_document_missing_its_type_is_counted_as_a_failure(
        self, processor, tmp_path
    ):
        """``_process_documents`` indexes ``doc["type"]``, so a malformed entry
        raises inside the per-document try and is counted rather than aborting
        the batch."""
        untyped = {"path": _write_pdf(tmp_path, "x.pdf"), "filename": "x.pdf"}

        result = processor._process_documents([untyped], "b", "cli-batch", "inline")

        assert result == {
            "batch_id": "b",
            "document_ids": [],
            "uploaded": 0,
            "queued": 0,
            "failed": 1,
            "baselines_uploaded": 0,
            "source": "inline",
            "output_prefix": "cli-batch",
            "timestamp": result["timestamp"],
        }

    def test_the_batch_metadata_record_is_written_to_the_output_bucket(
        self, processor, s3, tmp_path
    ):
        """``get_batch_info`` and every status call read this object back, so its
        key and its contents are the batch's only durable record."""
        _write_pdf(tmp_path, "a.pdf")

        result = processor.process_batch_from_directory(str(tmp_path), batch_id="b")

        stored = s3.get_object(Bucket=OUTPUT_BUCKET, Key="cli-batches/b/metadata.json")
        assert stored["ContentType"] == "application/json"
        assert json.loads(stored["Body"].read()) == result

    def test_the_timestamp_is_an_iso_utc_instant(self, processor, tmp_path):
        """``operations.batch`` parses this with ``datetime.fromisoformat`` into a
        ``BatchProcessResult``, so a non-ISO value breaks the public API."""
        from datetime import datetime

        _write_pdf(tmp_path, "a.pdf")

        result = processor.process_batch_from_directory(str(tmp_path), batch_id="b")

        parsed = datetime.fromisoformat(result["timestamp"])
        assert parsed.utcoffset() is not None
        assert parsed.utcoffset().total_seconds() == 0
