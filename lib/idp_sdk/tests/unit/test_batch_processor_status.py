# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the read side of ``idp_sdk._core.batch_processor``.

Once a batch has been submitted, the same class is how a caller finds it again:
``get_batch_info`` reads back the metadata record written at submission time,
``list_batches`` pages over the batch prefixes in the output bucket,
``download_batch_results`` pulls the pipeline's output files down to a local
tree, and the two version methods list a document's processing runs in DynamoDB
and download the exact bytes one run produced, pinned by S3 object version.

Three things shaped these tests.

**Everything runs against a real fake.** A moto CloudFormation stack supplies the
resource names through the production ``StackInfo`` path, so the processor is
built exactly as it is in production, and the S3 buckets and the DynamoDB table
really exist. That matters most for the parts a mock cannot judge: a
``ContinuationToken`` round-trip through base64, a DynamoDB ``KeyConditionExpression``
and its ``ScanIndexForward`` ordering, a paginated query whose second page is only
reached because the first hit the 1 MB response limit, and a ``download_file``
that must resolve a *superseded* object version rather than the current one.

**Version pinning is asserted by reading back older bytes.** A download that
silently returned the newest object would look identical to a correct one under a
mock, and would misreport which run produced which result.

**The path-containment check is exercised with keys that try to escape.** A
manifest is data read out of S3; a key of ``../`` or an absolute path must not
write outside the caller's output directory.

``list_document_versions`` is unusable as shipped — see
``test_list_document_versions_raises_keyerror_on_a_real_stack``.
"""

import base64
import json
import os

import boto3
import pytest
from moto import mock_aws

from idp_sdk._core.batch_processor import BatchListDict, BatchProcessor

STACK_NAME = "test-idp-stack"
INPUT_BUCKET = "idp-input-bucket"
OUTPUT_BUCKET = "idp-output-bucket"
TRACKING_TABLE = "idp-tracking-table"

_TEMPLATE = json.dumps(
    {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {
            "DocumentQueue": {
                "Type": "AWS::SQS::Queue",
                "Properties": {"QueueName": "idp-document-queue"},
            },
            "TrackingTable": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {
                    "TableName": TRACKING_TABLE,
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
        "Outputs": {
            "S3InputBucketName": {"Value": INPUT_BUCKET},
            "S3OutputBucketName": {"Value": OUTPUT_BUCKET},
            "LambdaLookupFunctionName": {"Value": "idp-lookup-function"},
        },
    }
)


@pytest.fixture
def idp_stack(aws_credentials):
    """A live moto account holding the stack, its buckets and its table."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name=aws_credentials)
        cfn.create_stack(StackName=STACK_NAME, TemplateBody=_TEMPLATE)
        s3_client = boto3.client("s3", region_name=aws_credentials)
        for bucket in (INPUT_BUCKET, OUTPUT_BUCKET):
            s3_client.create_bucket(Bucket=bucket)
        yield aws_credentials


@pytest.fixture
def processor(idp_stack):
    return BatchProcessor(stack_name=STACK_NAME, region=idp_stack)


@pytest.fixture
def s3(idp_stack):
    return boto3.client("s3", region_name=idp_stack)


@pytest.fixture
def tracking_table(idp_stack):
    """The real DynamoDB table the stack declares, as a resource-level Table."""
    return boto3.resource("dynamodb", region_name=idp_stack).Table(TRACKING_TABLE)


def _store_batch(s3_client, batch_id, document_ids, **extra):
    """Write a batch metadata record where ``get_batch_info`` looks for it."""
    record = {
        "batch_id": batch_id,
        "document_ids": document_ids,
        "queued": len(document_ids),
        "failed": 0,
        "timestamp": "2026-01-01T00:00:00+00:00",
        **extra,
    }
    s3_client.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=f"cli-batches/{batch_id}/metadata.json",
        Body=json.dumps(record),
        ContentType="application/json",
    )
    return record


def _local_tree(root):
    """Every file below ``root``, as paths relative to it, sorted."""
    found = []
    for directory, _subdirs, files in os.walk(root):
        for name in files:
            full = os.path.join(directory, name)
            found.append(os.path.relpath(full, root))
    return sorted(found)


@pytest.mark.unit
@pytest.mark.batch
class TestBatchListDict:
    """The list/dict hybrid ``list_batches`` returns.

    Callers written against the older API index and iterate it as a list, while
    the pagination fields are only reachable as dict keys. Both readings have to
    keep working off one object.
    """

    def test_iteration_yields_batches_not_dict_keys(self):
        listing = BatchListDict({"batches": [{"batch_id": "a"}], "count": 1})

        assert list(listing) == [{"batch_id": "a"}]

    def test_len_reports_the_count_field(self):
        listing = BatchListDict({"batches": [{"batch_id": "a"}], "count": 1})

        assert len(listing) == 1

    def test_an_integer_index_selects_a_batch(self):
        listing = BatchListDict({"batches": [{"batch_id": "a"}, {"batch_id": "b"}]})

        assert listing[1] == {"batch_id": "b"}

    def test_a_string_key_still_reads_the_dict(self):
        cursor = base64.b64encode(b"2").decode()
        listing = BatchListDict({"batches": [], "count": 0, "next_token": cursor})

        assert listing["next_token"] == cursor
        assert listing["batches"] == []

    def test_an_absent_dict_key_still_raises(self):
        with pytest.raises(KeyError):
            BatchListDict({"batches": []})["next_token"]

    def test_an_empty_listing_is_falsy_by_length(self):
        assert len(BatchListDict({"batches": [], "count": 0})) == 0


@pytest.mark.unit
@pytest.mark.batch
class TestGetBatchInfo:
    """Reading back the record written at submission time."""

    def test_a_stored_record_round_trips(self, processor, s3):
        record = _store_batch(s3, "batch-1", ["batch-1/a.pdf", "batch-1/b.pdf"])

        assert processor.get_batch_info("batch-1") == record

    def test_an_unknown_batch_is_reported_as_absent_not_as_an_error(
        self, processor, caplog
    ):
        """Callers branch on ``None`` to raise ``IDPResourceNotFoundError``, so an
        absent batch must come back as ``None`` and be logged as a warning rather
        than as an error."""
        with caplog.at_level("WARNING"):
            assert processor.get_batch_info("never-submitted") is None

        assert "Batch metadata not found" in caplog.text

    def test_a_broken_output_bucket_is_swallowed_as_absent(self, processor, caplog):
        """A failure that is not "no such key" also returns ``None`` — which means
        an infrastructure problem is indistinguishable from an unknown batch to
        the caller, and shows up only as an ERROR log line."""
        processor.resources["OutputBucket"] = "bucket-that-does-not-exist"

        with caplog.at_level("ERROR"):
            assert processor.get_batch_info("batch-1") is None

        assert "Error retrieving batch metadata" in caplog.text


@pytest.mark.unit
@pytest.mark.batch
class TestListBatches:
    """Paging over the batch prefixes in the output bucket."""

    def test_an_unlimited_listing_is_newest_first(self, processor, s3):
        """Ordering is by name descending, which is only "newest first" because
        the generated batch id ends in a sortable UTC timestamp. This holds when
        every batch fits in one page; see the truncation test below for what
        happens when it does not."""
        for batch_id in ("run-20260101-000000", "run-20260301-000000"):
            _store_batch(s3, batch_id, [f"{batch_id}/a.pdf"])

        listing = processor.list_batches()

        assert [b["batch_id"] for b in listing["batches"]] == [
            "run-20260301-000000",
            "run-20260101-000000",
        ]
        assert listing["count"] == 2
        assert "next_token" not in listing

    def test_a_prefix_with_no_metadata_record_is_left_out(self, processor, s3):
        """A batch directory holding only result files — a partially deleted
        batch, or one whose metadata write failed — is skipped rather than
        surfaced as an entry with no fields."""
        _store_batch(s3, "batch-good", ["batch-good/a.pdf"])
        s3.put_object(
            Bucket=OUTPUT_BUCKET, Key="cli-batches/batch-orphan/other.json", Body=b"{}"
        )

        listing = processor.list_batches()

        assert [b["batch_id"] for b in listing["batches"]] == ["batch-good"]
        assert listing["count"] == 1

    def test_the_limit_truncates_and_hands_back_a_resumable_cursor(self, processor, s3):
        """The cursor round-trips, and the ordering is a DEFECT pinned as-is:
        ``batch_processor.py:876-882``.

        The cursor is the S3 continuation token in base64, and passing it back
        must yield the rest with no overlap and nothing skipped — that part
        holds. What does not hold is "most recent first": the sort is applied to
        the page S3 already chose, and S3 lists ascending, so page one holds the
        **oldest** two batches ordered newest-first among themselves, and the
        newest batch (``batch-3``) is only on page two.

        The consequence is a wrong answer that looks right: the default
        ``limit=10`` on a stack with more batches than that returns the ten
        oldest, presented in descending order. A caller looking for their last
        run does not find it and has no reason to page.

        Fixing it means collecting every prefix before sorting and slicing; this
        test then fails, and page one becomes ``batch-3``, ``batch-2``.
        """
        for name in ("batch-1", "batch-2", "batch-3"):
            _store_batch(s3, name, [f"{name}/a.pdf"])

        first = processor.list_batches(limit=2)
        assert [b["batch_id"] for b in first["batches"]] == ["batch-2", "batch-1"]
        cursor = first["next_token"]

        second = processor.list_batches(limit=2, next_token=cursor)

        assert [b["batch_id"] for b in second["batches"]] == ["batch-3"]
        assert "next_token" not in second

    def test_the_cursor_is_base64_of_the_s3_token(self, processor, s3):
        """Pinned because the token is decoded with ``base64.b64decode`` before
        being handed to S3: an un-encoded token would be rejected by S3, and a
        doubly encoded one would silently restart from the beginning."""
        import base64

        for name in ("batch-1", "batch-2"):
            _store_batch(s3, name, [f"{name}/a.pdf"])

        cursor = processor.list_batches(limit=1)["next_token"]

        decoded = base64.b64decode(cursor).decode("utf-8")
        assert decoded and decoded != cursor

    def test_an_unreadable_bucket_yields_an_empty_listing(self, processor, caplog):
        """``list_batches`` never raises; a failure is an empty listing. Pinned so
        the swallowing is a decision on record rather than a surprise, because a
        caller cannot tell "no batches" from "could not look"."""
        processor.resources["OutputBucket"] = "bucket-that-does-not-exist"

        with caplog.at_level("ERROR"):
            listing = processor.list_batches()

        assert isinstance(listing, BatchListDict)
        assert listing["batches"] == []
        assert len(listing) == 0
        assert "Error listing batches" in caplog.text


@pytest.mark.unit
@pytest.mark.batch
class TestDownloadBatchResults:
    """Pulling a batch's output files into a local tree."""

    @staticmethod
    def _put_outputs(s3_client):
        keys = [
            "batch-1/a.pdf/pages/1/text.json",
            "batch-1/a.pdf/sections/1/result.json",
            "batch-1/a.pdf/summary/summary.json",
            "batch-1/b.pdf/sections/1/result.json",
            "batch-1/b.pdf/evaluation/report.json",
        ]
        for key in keys:
            s3_client.put_object(
                Bucket=OUTPUT_BUCKET, Key=key, Body=key.encode("utf-8")
            )
        return keys

    def test_all_selects_every_file_and_preserves_the_key_as_the_local_path(
        self, processor, s3, tmp_path
    ):
        keys = self._put_outputs(s3)
        destination = tmp_path / "results"

        stats = processor.download_batch_results("batch-1", str(destination), ["all"])

        assert stats["files_downloaded"] == len(keys)
        # Two distinct documents contributed files.
        assert stats["documents_downloaded"] == 2
        assert stats["output_dir"] == str(destination)
        assert _local_tree(destination) == sorted(
            key.replace("/", os.sep) for key in keys
        )
        # The bytes, not just the paths.
        landed = destination / "batch-1" / "a.pdf" / "sections" / "1" / "result.json"
        assert landed.read_bytes() == b"batch-1/a.pdf/sections/1/result.json"

    def test_a_file_type_filter_matches_a_whole_path_segment(
        self, processor, s3, tmp_path
    ):
        self._put_outputs(s3)
        destination = tmp_path / "results"

        stats = processor.download_batch_results(
            "batch-1", str(destination), ["sections"]
        )

        assert stats["files_downloaded"] == 2
        assert _local_tree(destination) == sorted(
            key.replace("/", os.sep)
            for key in (
                "batch-1/a.pdf/sections/1/result.json",
                "batch-1/b.pdf/sections/1/result.json",
            )
        )

    def test_several_file_types_are_unioned(self, processor, s3, tmp_path):
        self._put_outputs(s3)
        destination = tmp_path / "results"

        stats = processor.download_batch_results(
            "batch-1", str(destination), ["summary", "evaluation"]
        )

        assert stats["files_downloaded"] == 2

    def test_a_batch_with_no_output_downloads_nothing_and_still_creates_the_dir(
        self, processor, tmp_path
    ):
        destination = tmp_path / "results"

        stats = processor.download_batch_results("absent", str(destination), ["all"])

        assert stats == {
            "files_downloaded": 0,
            "documents_downloaded": 0,
            "output_dir": str(destination),
        }
        assert destination.is_dir()

    def test_only_the_named_batch_prefix_is_downloaded(self, processor, s3, tmp_path):
        self._put_outputs(s3)
        s3.put_object(
            Bucket=OUTPUT_BUCKET, Key="batch-2/c.pdf/sections/1/result.json", Body=b"c"
        )
        destination = tmp_path / "results"

        processor.download_batch_results("batch-1", str(destination), ["sections"])

        assert all(path.startswith("batch-1") for path in _local_tree(destination)), (
            _local_tree(destination)
        )


@pytest.mark.unit
@pytest.mark.batch
class TestListDocumentVersions:
    """Listing a document's processing runs out of the tracking table."""

    def test_list_document_versions_raises_keyerror_on_a_real_stack(self, processor):
        """DEFECT, pinned as-is: ``batch_processor.py:1019``.

        The method reads ``self.resources["TrackingTable"]``, but the resource map
        ``StackInfo.get_resources`` builds has no such key — it records the very
        same table under ``DocumentsTable`` (``stack_info.py:66``). ``resources``
        comes from ``StackInfo`` and from nowhere else, so the lookup can never
        succeed and the method raises ``KeyError: 'TrackingTable'`` for every
        input against every stack.

        The observable consequence reaches the public API: ``client.batch.list_versions``
        does not wrap exceptions, so a caller gets a bare ``KeyError`` naming an
        internal resource key instead of a version list. ``batch.download_version``
        is unaffected — it reads the manifest from S3 and never touches the table.

        When it is fixed, this test fails; the assertions in the rest of this class
        (which inject the key the code asks for) describe the intended behaviour.
        """
        with pytest.raises(KeyError, match="TrackingTable"):
            processor.list_document_versions("batch-1/a.pdf")

    @pytest.fixture
    def versioned(self, processor):
        """The processor with the resource key the method asks for, so the query
        itself can be tested. See the defect above for why this is needed."""
        processor.resources["TrackingTable"] = TRACKING_TABLE
        return processor

    def test_runs_come_back_newest_first(self, versioned, tracking_table):
        for run in ("001", "002", "003"):
            tracking_table.put_item(
                Item={
                    "PK": "doc#batch-1/a.pdf",
                    "SK": f"run#{run}",
                    "RunId": run,
                    "ConfigVersion": "profile-a",
                }
            )

        runs = versioned.list_document_versions("batch-1/a.pdf")

        assert [item["RunId"] for item in runs] == ["003", "002", "001"]
        assert runs[0]["ConfigVersion"] == "profile-a"

    def test_only_run_records_for_that_document_are_returned(
        self, versioned, tracking_table
    ):
        """The table holds more than runs under the same partition, and other
        documents' runs under their own. A key condition that matched either
        would attribute another document's results to this one."""
        tracking_table.put_item(
            Item={"PK": "doc#batch-1/a.pdf", "SK": "run#001", "RunId": "001"}
        )
        tracking_table.put_item(
            Item={"PK": "doc#batch-1/a.pdf", "SK": "meta#latest", "RunId": "not-a-run"}
        )
        tracking_table.put_item(
            Item={"PK": "doc#batch-1/b.pdf", "SK": "run#009", "RunId": "009"}
        )

        runs = versioned.list_document_versions("batch-1/a.pdf")

        assert [item["RunId"] for item in runs] == ["001"]

    def test_a_document_with_no_runs_returns_an_empty_list(self, versioned):
        assert versioned.list_document_versions("batch-1/never-run.pdf") == []

    def test_every_page_of_a_long_history_is_returned(self, versioned, tracking_table):
        """A DynamoDB query answers at most 1 MB per call, so a document with a
        long history needs the ``LastEvaluatedKey`` loop. The items here are
        padded so the first response really is truncated — the loop cannot be
        exercised by item count alone, and a query that ignored the continuation
        would silently report only the most recent runs.
        """
        padding = "x" * 40_000
        total = 40
        with tracking_table.batch_writer() as writer:
            for index in range(total):
                writer.put_item(
                    Item={
                        "PK": "doc#batch-1/a.pdf",
                        "SK": f"run#{index:03d}",
                        "RunId": f"{index:03d}",
                        "Padding": padding,
                    }
                )

        runs = versioned.list_document_versions("batch-1/a.pdf")

        assert len(runs) == total
        assert [item["RunId"] for item in runs] == [
            f"{index:03d}" for index in reversed(range(total))
        ]


@pytest.mark.unit
@pytest.mark.batch
class TestDownloadVersionResults:
    """Downloading the exact bytes one processing run produced."""

    @staticmethod
    def _put_manifest(s3_client, document_id, run_id, files):
        s3_client.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=f"{document_id}/runs/{run_id}/manifest.json",
            Body=json.dumps({"files": files}).encode("utf-8"),
        )

    def test_a_missing_manifest_names_the_version_that_has_none(
        self, processor, tmp_path
    ):
        with pytest.raises(ValueError, match="No manifest found for version run-9"):
            processor.download_version_results(
                "batch-1/a.pdf", "run-9", str(tmp_path / "out")
            )

    def test_a_pinned_version_downloads_the_superseded_bytes(
        self, processor, s3, tmp_path
    ):
        """This is the whole point of the method: after a re-run has overwritten
        the output objects, the earlier run's download must still produce the
        earlier run's bytes. Returning the current object would look like a
        success and misattribute results to the wrong run.
        """
        s3.put_bucket_versioning(
            Bucket=OUTPUT_BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        key = "batch-1/a.pdf/sections/1/result.json"
        first = s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=b"run-1 output")
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=b"run-2 output")
        self._put_manifest(
            s3,
            "batch-1/a.pdf",
            "run-1",
            [{"key": key, "version_id": first["VersionId"]}],
        )
        destination = tmp_path / "out"

        stats = processor.download_version_results(
            "batch-1/a.pdf", "run-1", str(destination)
        )

        assert stats == {
            "files_downloaded": 1,
            "run_id": "run-1",
            "output_dir": os.path.join(str(destination), "run-1"),
        }
        landed = destination / "run-1" / key
        assert landed.read_bytes() == b"run-1 output"

    def test_an_unversioned_entry_takes_the_current_object(
        self, processor, s3, tmp_path
    ):
        """On a bucket without versioning S3 reports the version id as the string
        ``"null"``, which must not be sent as a ``VersionId``."""
        key = "batch-1/a.pdf/summary/summary.json"
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=b"only version")
        self._put_manifest(
            s3, "batch-1/a.pdf", "run-1", [{"key": key, "version_id": "null"}]
        )
        destination = tmp_path / "out"

        stats = processor.download_version_results(
            "batch-1/a.pdf", "run-1", str(destination)
        )

        assert stats["files_downloaded"] == 1
        assert (destination / "run-1" / key).read_bytes() == b"only version"

    def test_an_entry_with_no_version_field_still_downloads(
        self, processor, s3, tmp_path
    ):
        key = "batch-1/a.pdf/summary/summary.json"
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=b"latest")
        self._put_manifest(s3, "batch-1/a.pdf", "run-1", [{"key": key}])
        destination = tmp_path / "out"

        stats = processor.download_version_results(
            "batch-1/a.pdf", "run-1", str(destination)
        )

        assert stats["files_downloaded"] == 1
        assert (destination / "run-1" / key).read_bytes() == b"latest"

    def test_a_manifest_key_that_escapes_the_output_directory_is_skipped(
        self, processor, s3, tmp_path, caplog
    ):
        """The manifest is data read out of S3, so its keys are not trusted. A
        relative escape and an absolute path must both be refused, while the
        legitimate entry in the same manifest still lands — a failure here writes
        a file the caller never asked for, outside the directory they named.
        """
        good_key = "batch-1/a.pdf/sections/1/result.json"
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=good_key, Body=b"legitimate")
        self._put_manifest(
            s3,
            "batch-1/a.pdf",
            "run-1",
            [
                {"key": "../../escaped.json"},
                {"key": os.path.join(str(tmp_path), "absolute.json")},
                {"key": good_key},
            ],
        )
        destination = tmp_path / "out"
        destination.mkdir()

        with caplog.at_level("WARNING"):
            stats = processor.download_version_results(
                "batch-1/a.pdf", "run-1", str(destination)
            )

        assert stats["files_downloaded"] == 1
        assert _local_tree(destination) == [
            os.path.join("run-1", good_key.replace("/", os.sep))
        ]
        assert not (tmp_path / "absolute.json").exists()
        assert not (tmp_path / "escaped.json").exists()
        assert caplog.text.count("Skipping unsafe manifest key") == 2

    def test_a_manifest_with_no_files_creates_the_directory_and_stops(
        self, processor, s3, tmp_path
    ):
        self._put_manifest(s3, "batch-1/a.pdf", "run-1", [])
        destination = tmp_path / "out"

        stats = processor.download_version_results(
            "batch-1/a.pdf", "run-1", str(destination)
        )

        assert stats["files_downloaded"] == 0
        assert destination.is_dir()
        assert _local_tree(destination) == []
