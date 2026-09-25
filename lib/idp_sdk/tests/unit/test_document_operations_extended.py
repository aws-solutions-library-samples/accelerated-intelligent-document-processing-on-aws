# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the rest of ``idp_sdk.operations.document``.

``DocumentOperation`` is the public single-document namespace on ``IDPClient``:
``process`` uploads a local file into the stack's input bucket, ``get_status``
reads one document out of the batch-status monitor, ``download_results`` and
``download_source`` pull objects back out of S3, ``reprocess``/``rerun`` requeue a
document from a pipeline step, ``delete`` removes a document and everything
derived from it, and ``get_metadata`` reads one section's extracted fields.
``tests/unit/test_document_list_operation.py`` already covers ``list``.

**What shaped these tests.** Four of these methods are S3-shaped, and an S3 key
is exactly the kind of thing a ``MagicMock`` will accept while being wrong: a
mock records ``upload_file(Bucket=..., Key=...)`` happily whether the key carries
the batch prefix or not, and a mocked paginator cannot show that the
type filter in ``download_results`` does nothing on its default arguments. So the
S3 and DynamoDB paths here run against ``moto``, on top of a real (moto)
CloudFormation stack that ``idp_sdk._core.stack_info.StackInfo`` discovers for
itself — the resource names the operations use are read out of stack outputs, not
handed to them — and the assertions read the bucket and the table back
afterwards. The two methods that delegate to a ``_core`` processor
(``reprocess``, ``get_metadata``) are covered by asserting the arguments the
processor received *and* the values the wrapper returned from a realistic
processor return value.

Two defects are pinned rather than fixed; each is marked in the test's own
docstring.
"""

import json
import os
import re
import warnings

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk.exceptions import (
    IDPProcessingError,
    IDPResourceNotFoundError,
)
from idp_sdk.models import (
    DocumentDownloadResult,
    DocumentMetadata,
    DocumentReprocessResult,
    DocumentStatus,
    DocumentUploadResult,
    RerunStep,
)

pytestmark = pytest.mark.unit

STACK_NAME = "idp-doc-test"
INPUT_BUCKET = "idp-doc-test-input"
OUTPUT_BUCKET = "idp-doc-test-output"
TRACKING_TABLE = "idp-doc-test-tracking"

#: The minimum stack shape ``StackInfo`` needs: it reads bucket names from stack
#: outputs, the queue URL from the ``DocumentQueue`` resource's physical id, and
#: the tracking table name from the ``TrackingTable`` resource.
STACK_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {
        "DocumentQueue": {
            "Type": "AWS::SQS::Queue",
            "Properties": {"QueueName": "idp-doc-test-queue"},
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
        "LambdaLookupFunctionName": {"Value": "idp-doc-test-lookup"},
    },
}


@pytest.fixture
def idp_stack(aws_credentials, aws_region):
    """A live (moto) IDP stack: CloudFormation outputs, two buckets, one table.

    Yields the ``IDPClient`` bound to it. The output bucket has versioning
    enabled because that is how the real template declares it, and
    ``delete_documents`` purges *versions* rather than issuing versionless
    deletes — a non-versioned fixture would let a broken purge look successful.
    """
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name=aws_region)
        cfn.create_stack(StackName=STACK_NAME, TemplateBody=json.dumps(STACK_TEMPLATE))
        s3 = boto3.client("s3", region_name=aws_region)
        s3.create_bucket(Bucket=INPUT_BUCKET)
        s3.create_bucket(Bucket=OUTPUT_BUCKET)
        s3.put_bucket_versioning(
            Bucket=OUTPUT_BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        yield IDPClient(stack_name=STACK_NAME, region=aws_region)


def _s3(region):
    return boto3.client("s3", region_name=region)


# ---------------------------------------------------------------------------
# process()
# ---------------------------------------------------------------------------


class TestProcess:
    def test_the_file_reaches_the_input_bucket_under_a_batch_prefix(
        self, idp_stack, aws_region, tmp_path
    ):
        """The bytes must land in the bucket the stack output names, at the key
        the result reports. Asserting the return value alone would pass on an
        upload that never happened."""
        local = tmp_path / "invoice.pdf"
        local.write_bytes(b"%PDF-1.7 invoice")

        result = idp_stack.document.process(file_path=str(local))

        assert isinstance(result, DocumentUploadResult)
        assert result.status == "queued"
        # "single-doc-YYYYmmdd-HHMMSS/invoice.pdf"
        assert re.fullmatch(
            r"single-doc-\d{8}-\d{6}/invoice\.pdf", result.document_id
        ), result.document_id

        stored = _s3(aws_region).get_object(Bucket=INPUT_BUCKET, Key=result.document_id)
        assert stored["Body"].read() == b"%PDF-1.7 invoice"

    def test_a_custom_document_id_is_silently_discarded(
        self, idp_stack, aws_region, tmp_path
    ):
        """DEFECT (operations/document.py:62-77). ``process`` documents
        ``document_id`` as an "optional custom document ID", computes a default
        for it, and then never uses it: the S3 key is always
        ``{batch_id}/{filename}`` and that key is what is returned. A caller who
        passes an id gets no error and no effect — their chosen id appears
        neither in S3 nor in the result — so any bookkeeping keyed on it will not
        match the document the pipeline actually processes. Pinned as-is.
        """
        local = tmp_path / "invoice.pdf"
        local.write_bytes(b"data")

        result = idp_stack.document.process(
            file_path=str(local), document_id="my-own-id"
        )

        assert "my-own-id" not in result.document_id
        assert result.document_id.endswith("/invoice.pdf")
        keys = [
            o["Key"]
            for o in _s3(aws_region).list_objects_v2(Bucket=INPUT_BUCKET)["Contents"]
        ]
        assert keys == [result.document_id]

    def test_a_missing_local_file_is_a_processing_error(self, idp_stack, tmp_path):
        with pytest.raises(IDPProcessingError, match="File not found"):
            idp_stack.document.process(file_path=str(tmp_path / "absent.pdf"))

    def test_an_upload_failure_is_wrapped_not_leaked(
        self, idp_stack, aws_region, tmp_path
    ):
        """A botocore error must surface as the SDK's own exception type; a
        caller catching ``IDPProcessingError`` should not also have to catch
        ``ClientError``."""
        local = tmp_path / "invoice.pdf"
        local.write_bytes(b"data")
        _s3(aws_region).delete_bucket(Bucket=INPUT_BUCKET)

        with pytest.raises(IDPProcessingError, match="Failed to upload document"):
            idp_stack.document.process(file_path=str(local))

    def test_a_missing_stack_name_is_refused_before_any_io(self, aws_credentials):
        client = IDPClient()
        with pytest.raises(Exception, match="stack_name is required"):
            client.document.process(file_path="/nonexistent/x.pdf")


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------


#: The shape ``ProgressMonitor._batch_query_documents`` really produces: every
#: category key present, timings as empty strings when the document has none,
#: ``duration`` a float in seconds, and no page or section counts at all.
def _status_summary(**categories):
    summary = {"completed": [], "running": [], "queued": [], "failed": []}
    summary.update(categories)
    summary["total"] = sum(len(v) for v in summary.values() if isinstance(v, list))
    summary["all_complete"] = not (summary["running"] or summary["queued"])
    return summary


def _patch_monitor(monkeypatch, status_summary):
    """Stand in for BatchProcessor/ProgressMonitor, recording their arguments."""
    calls = {}

    class FakeProcessor:
        def __init__(self, stack_name, region=None, **kwargs):
            calls["processor"] = {"stack_name": stack_name, "region": region}
            self.resources = {"LookupFunctionName": "idp-doc-test-lookup"}

    class FakeMonitor:
        def __init__(self, stack_name, resources, region=None):
            calls["monitor"] = {
                "stack_name": stack_name,
                "resources": resources,
                "region": region,
            }

        def get_batch_status(self, document_ids):
            calls["document_ids"] = document_ids
            return status_summary

    monkeypatch.setattr("idp_sdk._core.batch_processor.BatchProcessor", FakeProcessor)
    monkeypatch.setattr("idp_sdk._core.progress_monitor.ProgressMonitor", FakeMonitor)
    return calls


class TestGetStatus:
    def test_a_completed_document_maps_every_field(self, aws_credentials, monkeypatch):
        calls = _patch_monitor(
            monkeypatch,
            _status_summary(
                completed=[
                    {
                        "document_id": "batch-1/invoice.pdf",
                        "status": "COMPLETED",
                        "start_time": "2024-01-15T10:30:00",
                        "end_time": "2024-01-15T10:30:42",
                        "duration": 42.5,
                        "num_pages": 3,
                        "num_sections": 2,
                    }
                ]
            ),
        )

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        status = client.document.get_status("batch-1/invoice.pdf")

        assert isinstance(status, DocumentStatus)
        assert status.document_id == "batch-1/invoice.pdf"
        assert status.status == "COMPLETED"
        assert status.duration_seconds == 42.5
        assert status.num_pages == 3
        assert status.num_sections == 2
        assert status.error is None
        assert status.start_time is not None and status.start_time.minute == 30
        assert status.end_time is not None and status.end_time.second == 42
        # Only the one document is queried, and the monitor is wired to the
        # processor's discovered resources rather than to a fresh lookup.
        assert calls["document_ids"] == ["batch-1/invoice.pdf"]
        assert calls["monitor"]["resources"] == {
            "LookupFunctionName": "idp-doc-test-lookup"
        }
        assert calls["monitor"]["stack_name"] == STACK_NAME

    def test_empty_timestamps_become_none_rather_than_failing_validation(
        self, aws_credentials, monkeypatch
    ):
        """``_batch_query_documents`` emits ``""`` for a document that has not
        started. ``DocumentStatus.start_time`` is a ``datetime``, so without the
        normalisation every in-flight document would raise a pydantic
        ValidationError instead of reporting its status."""
        _patch_monitor(
            monkeypatch,
            _status_summary(
                running=[
                    {
                        "document_id": "batch-1/slow.pdf",
                        "status": "RUNNING",
                        "start_time": "",
                        "end_time": "",
                        "duration": 0,
                    }
                ]
            ),
        )

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        status = client.document.get_status("batch-1/slow.pdf")

        assert status.status == "RUNNING"
        assert status.start_time is None
        assert status.end_time is None
        assert status.duration_seconds == 0

    def test_a_failed_document_carries_its_error(self, aws_credentials, monkeypatch):
        _patch_monitor(
            monkeypatch,
            _status_summary(
                failed=[
                    {
                        "document_id": "batch-1/bad.pdf",
                        "status": "FAILED",
                        "start_time": "",
                        "end_time": "",
                        "duration": 0,
                        "error": "Textract threw InvalidDocumentException",
                    }
                ]
            ),
        )

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        status = client.document.get_status("batch-1/bad.pdf")

        assert status.status == "FAILED"
        assert status.error == "Textract threw InvalidDocumentException"

    def test_a_document_in_no_category_is_not_found(self, aws_credentials, monkeypatch):
        """An id the monitor does not know about must raise, not return a
        placeholder with status UNKNOWN."""
        _patch_monitor(
            monkeypatch,
            _status_summary(
                completed=[{"document_id": "batch-1/other.pdf", "status": "COMPLETED"}]
            ),
        )

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        with pytest.raises(IDPResourceNotFoundError, match="batch-1/invoice.pdf"):
            client.document.get_status("batch-1/invoice.pdf")


# ---------------------------------------------------------------------------
# download_results()
# ---------------------------------------------------------------------------

#: One document's output prefix, plus a sibling whose key shares the byte prefix.
OUTPUT_OBJECTS = {
    "invoice.pdf/pages/1/text.json": b'{"page": 1}',
    "invoice.pdf/pages/2/text.json": b'{"page": 2}',
    "invoice.pdf/sections/1/result.json": b'{"section": 1}',
    "invoice.pdf/summary.json": b'{"summary": true}',
    "invoice.pdf/metering.json": b'{"tokens": 10}',
}


class TestDownloadResults:
    @pytest.fixture
    def populated(self, idp_stack, aws_region):
        s3 = _s3(aws_region)
        for key, body in OUTPUT_OBJECTS.items():
            s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=body)
        return idp_stack

    def test_the_default_call_downloads_every_object_under_the_prefix(
        self, populated, tmp_path
    ):
        """DEFECT (operations/document.py:174). The type filter is guarded by
        ``if file_types and ...`` — the *argument*, not the ``types_list``
        default — so on the documented default call (``file_types=None``) no
        filtering happens at all and the four-type default list is dead. A caller
        relying on the default gets ``metering.json`` and anything else under the
        prefix too. Pinned as-is: the observable consequence is extra files and a
        larger ``files_downloaded`` count, not a wrong one.
        """
        out = tmp_path / "results"

        result = populated.document.download_results(
            document_id="invoice.pdf", output_dir=str(out)
        )

        assert isinstance(result, DocumentDownloadResult)
        assert result.files_downloaded == len(OUTPUT_OBJECTS)
        assert result.output_dir == str(out)
        # metering.json is in none of the four default types, and is here anyway.
        assert (out / "invoice.pdf" / "metering.json").is_file()

    def test_files_land_under_the_full_s3_key_beneath_the_output_dir(
        self, populated, tmp_path
    ):
        """The local layout mirrors the S3 key, document prefix included, so two
        documents downloaded into one directory cannot collide."""
        out = tmp_path / "results"

        populated.document.download_results(
            document_id="invoice.pdf", output_dir=str(out)
        )

        assert (out / "invoice.pdf" / "pages" / "1" / "text.json").read_bytes() == (
            b'{"page": 1}'
        )
        assert (out / "invoice.pdf" / "sections" / "1" / "result.json").is_file()

    def test_an_explicit_type_list_selects_by_directory_or_filename(
        self, populated, tmp_path
    ):
        """``pages`` matches the ``/pages/`` directory segment and ``summary``
        matches the ``/summary.json`` leaf; the two shapes are different branches
        of the same predicate."""
        out = tmp_path / "results"

        result = populated.document.download_results(
            document_id="invoice.pdf",
            output_dir=str(out),
            file_types=["pages", "summary"],
        )

        assert result.files_downloaded == 3  # 2 pages + summary.json
        assert (out / "invoice.pdf" / "summary.json").is_file()
        assert not (out / "invoice.pdf" / "metering.json").exists()
        assert not (out / "invoice.pdf" / "sections").exists()

    def test_the_literal_all_disables_filtering(self, populated, tmp_path):
        out = tmp_path / "results"

        result = populated.document.download_results(
            document_id="invoice.pdf", output_dir=str(out), file_types=["all"]
        )

        assert result.files_downloaded == len(OUTPUT_OBJECTS)

    def test_a_document_with_no_output_downloads_nothing_without_raising(
        self, populated, tmp_path
    ):
        """Nothing under the prefix is an empty result, not an error: a document
        still being processed has no output yet."""
        out = tmp_path / "results"

        result = populated.document.download_results(
            document_id="never-processed.pdf", output_dir=str(out)
        )

        assert result.files_downloaded == 0
        assert out.is_dir()

    def test_a_sibling_sharing_the_byte_prefix_is_left_alone(
        self, populated, aws_region, tmp_path
    ):
        """An S3 ``Prefix`` is a byte prefix, not a path segment, so
        ``invoice.pdf.bak/`` would be enumerated by a bare ``Prefix=invoice.pdf``.
        What keeps this path safe is the trailing slash the listing appends —
        ``Prefix=f"{document_id}/"`` — and that single character is the whole
        protection, which is why it is asserted rather than assumed.
        ``idp_common.delete_documents`` lists without it and needs a client-side
        predicate instead; there the same collision destroyed a sibling
        document's output history."""
        _s3(aws_region).put_object(
            Bucket=OUTPUT_BUCKET, Key="invoice.pdf.bak/summary.json", Body=b"{}"
        )
        out = tmp_path / "results"

        result = populated.document.download_results(
            document_id="invoice.pdf", output_dir=str(out), file_types=["summary"]
        )

        assert result.files_downloaded == 1
        assert (out / "invoice.pdf" / "summary.json").is_file()
        assert not (out / "invoice.pdf.bak").exists()

    def test_a_listing_failure_is_wrapped(self, idp_stack, aws_region, tmp_path):
        _s3(aws_region).delete_bucket(Bucket=OUTPUT_BUCKET)

        with pytest.raises(IDPProcessingError, match="Failed to download results"):
            idp_stack.document.download_results(
                document_id="invoice.pdf", output_dir=str(tmp_path / "out")
            )


# ---------------------------------------------------------------------------
# download_source()
# ---------------------------------------------------------------------------


class TestDownloadSource:
    def test_the_original_bytes_are_written_to_the_requested_path(
        self, idp_stack, aws_region, tmp_path
    ):
        _s3(aws_region).put_object(
            Bucket=INPUT_BUCKET, Key="batch-1/invoice.pdf", Body=b"%PDF original"
        )
        target = tmp_path / "nested" / "dir" / "invoice.pdf"

        returned = idp_stack.document.download_source(
            document_id="batch-1/invoice.pdf", output_path=str(target)
        )

        assert returned == str(target)
        assert target.read_bytes() == b"%PDF original"

    def test_a_bare_filename_needs_no_directory_creation(
        self, idp_stack, aws_region, tmp_path, monkeypatch
    ):
        """``os.path.dirname`` of a bare filename is ``""``, which
        ``os.makedirs`` would reject — hence the guard. Run from ``tmp_path`` so
        the relative write stays inside it."""
        _s3(aws_region).put_object(
            Bucket=INPUT_BUCKET, Key="invoice.pdf", Body=b"bytes"
        )
        monkeypatch.chdir(tmp_path)

        returned = idp_stack.document.download_source(
            document_id="invoice.pdf", output_path="invoice.pdf"
        )

        assert returned == "invoice.pdf"
        assert (tmp_path / "invoice.pdf").read_bytes() == b"bytes"

    def test_a_missing_key_is_a_resource_not_found_error(self, idp_stack, tmp_path):
        """``download_file`` HEADs first, so a missing key arrives as error code
        ``404`` rather than ``NoSuchKey``. Mapping only the latter would turn a
        missing document into a generic processing error."""
        with pytest.raises(IDPResourceNotFoundError, match="absent.pdf"):
            idp_stack.document.download_source(
                document_id="absent.pdf", output_path=str(tmp_path / "x.pdf")
            )

    def test_a_non_404_client_error_is_a_processing_error(
        self, idp_stack, tmp_path, aws_region
    ):
        _s3(aws_region).delete_bucket(Bucket=INPUT_BUCKET)

        with pytest.raises(IDPProcessingError, match="Failed to download source"):
            idp_stack.document.download_source(
                document_id="invoice.pdf", output_path=str(tmp_path / "x.pdf")
            )

    def test_a_non_client_error_is_also_wrapped(
        self, idp_stack, aws_region, tmp_path, monkeypatch
    ):
        """The second ``except`` exists for the non-botocore failures — a local
        write to an unwritable path, say — and must not leak either."""
        _s3(aws_region).put_object(
            Bucket=INPUT_BUCKET, Key="invoice.pdf", Body=b"bytes"
        )
        monkeypatch.setattr(
            os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
        )

        with pytest.raises(IDPProcessingError, match="read-only"):
            idp_stack.document.download_source(
                document_id="invoice.pdf", output_path=str(tmp_path / "sub" / "x.pdf")
            )


# ---------------------------------------------------------------------------
# reprocess() / rerun()
# ---------------------------------------------------------------------------


def _patch_rerun_processor(monkeypatch, return_value=None, raises=None):
    calls = {}

    class FakeRerunProcessor:
        def __init__(self, stack_name, region=None, **kwargs):
            calls["init"] = {"stack_name": stack_name, "region": region}

        def rerun_documents(self, document_ids, step, monitor):
            calls["rerun"] = {
                "document_ids": document_ids,
                "step": step,
                "monitor": monitor,
            }
            if raises is not None:
                raise raises
            return return_value

    monkeypatch.setattr(
        "idp_sdk._core.rerun_processor.RerunProcessor", FakeRerunProcessor
    )
    return calls


class TestReprocess:
    def test_a_queued_document_reports_its_step_and_arguments(
        self, aws_credentials, monkeypatch
    ):
        calls = _patch_rerun_processor(
            monkeypatch, return_value={"documents_queued": 1, "documents_failed": 0}
        )

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        result = client.document.reprocess("batch-1/invoice.pdf", step="classification")

        assert isinstance(result, DocumentReprocessResult)
        assert result.queued is True
        assert result.step is RerunStep.CLASSIFICATION
        assert result.document_id == "batch-1/invoice.pdf"
        # Exactly one document, never monitored — the SDK call is synchronous and
        # returns; a monitor=True here would block the caller.
        assert calls["rerun"] == {
            "document_ids": ["batch-1/invoice.pdf"],
            "step": "classification",
            "monitor": False,
        }
        assert calls["init"] == {"stack_name": STACK_NAME, "region": "us-east-1"}

    def test_a_rerunstep_enum_is_passed_through_as_its_value(
        self, aws_credentials, monkeypatch
    ):
        """The processor takes a string. Passing the enum object would reach it as
        ``RerunStep.EXTRACTION`` and not match any step name."""
        calls = _patch_rerun_processor(
            monkeypatch, return_value={"documents_queued": 1}
        )

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        result = client.document.reprocess("invoice.pdf", step=RerunStep.EXTRACTION)

        assert calls["rerun"]["step"] == "extraction"
        assert isinstance(calls["rerun"]["step"], str)
        assert result.step is RerunStep.EXTRACTION

    def test_zero_queued_is_reported_as_not_queued_rather_than_as_an_error(
        self, aws_credentials, monkeypatch
    ):
        _patch_rerun_processor(monkeypatch, return_value={"documents_queued": 0})

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        result = client.document.reprocess("invoice.pdf", step="extraction")

        assert result.queued is False

    def test_a_missing_queue_count_defaults_to_not_queued(
        self, aws_credentials, monkeypatch
    ):
        _patch_rerun_processor(monkeypatch, return_value={})

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        assert client.document.reprocess("invoice.pdf", step="extraction").queued is (
            False
        )

    def test_an_unknown_step_name_is_a_processing_error(
        self, aws_credentials, monkeypatch
    ):
        """``RerunStep("ocr")`` raises inside the try, so an invalid step arrives
        as the SDK's own error type with the offending value in the message."""
        _patch_rerun_processor(monkeypatch, return_value={"documents_queued": 1})

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        with pytest.raises(IDPProcessingError, match="ocr"):
            client.document.reprocess("invoice.pdf", step="ocr")

    def test_a_processor_failure_is_wrapped(self, aws_credentials, monkeypatch):
        _patch_rerun_processor(monkeypatch, raises=RuntimeError("state machine gone"))

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        with pytest.raises(IDPProcessingError, match="state machine gone"):
            client.document.reprocess("invoice.pdf", step="extraction")

    def test_rerun_warns_and_forwards_to_reprocess(self, aws_credentials, monkeypatch):
        """``rerun`` is the deprecated spelling. The warning matters as much as
        the forwarding: without it callers never learn to move."""
        calls = _patch_rerun_processor(
            monkeypatch, return_value={"documents_queued": 1}
        )

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = client.document.rerun("invoice.pdf", step="extraction")

        assert [w.category for w in caught] == [DeprecationWarning]
        assert "reprocess()" in str(caught[0].message)
        assert result.queued is True
        assert calls["rerun"]["document_ids"] == ["invoice.pdf"]


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.fixture
    def with_document(self, idp_stack, aws_region):
        """One document: an input object, output objects (two versions of one of
        them), a tracking record, a list entry and a run record — plus a sibling
        document whose key shares the byte prefix."""
        s3 = _s3(aws_region)
        s3.put_object(Bucket=INPUT_BUCKET, Key="invoice.pdf", Body=b"%PDF")
        s3.put_object(
            Bucket=OUTPUT_BUCKET, Key="invoice.pdf/sections/1/result.json", Body=b"v1"
        )
        s3.put_object(
            Bucket=OUTPUT_BUCKET, Key="invoice.pdf/sections/1/result.json", Body=b"v2"
        )
        s3.put_object(Bucket=OUTPUT_BUCKET, Key="invoice.pdf/summary.json", Body=b"s")
        s3.put_object(
            Bucket=OUTPUT_BUCKET, Key="invoice.pdf.bak/summary.json", Body=b"other"
        )

        table = boto3.resource("dynamodb", region_name=aws_region).Table(TRACKING_TABLE)
        queued = "2024-01-15T10:30:00.000000+00:00"
        table.put_item(
            Item={
                "PK": "doc#invoice.pdf",
                "SK": "none",
                "ObjectKey": "invoice.pdf",
                "QueuedTime": queued,
                "ObjectStatus": "COMPLETED",
            }
        )
        table.put_item(
            Item={
                "PK": "list#2024-01-15#s#10",
                "SK": f"ts#{queued}#id#invoice.pdf",
                "ObjectKey": "invoice.pdf",
            }
        )
        table.put_item(
            Item={"PK": "doc#invoice.pdf", "SK": "run#1", "ObjectKey": "invoice.pdf"}
        )
        return idp_stack, table

    def test_the_objects_and_records_are_really_gone(self, with_document, aws_region):
        """The point of running this against moto rather than a mock: a mock
        accepts a versionless delete on a versioned bucket, which leaves the
        bytes in place behind a delete marker. Only reading the bucket back
        afterwards can tell the difference."""
        client, table = with_document
        s3 = _s3(aws_region)

        result = client.document.delete("invoice.pdf")

        assert result.success is True
        assert result.object_key == "invoice.pdf"
        assert result.errors == []
        assert result.deleted["input_file"] is True
        # Three output versions purged: two of result.json plus summary.json.
        assert result.deleted["output_files"] == 3
        assert result.deleted["list_entries"] is True
        assert result.deleted["run_records"] == 1

        with pytest.raises(ClientError):
            s3.head_object(Bucket=INPUT_BUCKET, Key="invoice.pdf")
        remaining = s3.list_object_versions(Bucket=OUTPUT_BUCKET)
        surviving = {v["Key"] for v in remaining.get("Versions", [])}
        assert surviving == {"invoice.pdf.bak/summary.json"}
        assert remaining.get("DeleteMarkers", []) == []
        assert "Item" not in table.get_item(Key={"PK": "doc#invoice.pdf", "SK": "none"})
        assert (
            table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(
                    "doc#invoice.pdf"
                )
            )["Count"]
            == 0
        )

    def test_a_dry_run_deletes_nothing(self, with_document, aws_region):
        client, table = with_document

        result = client.document.delete("invoice.pdf", dry_run=True)

        assert result.success is True
        assert result.deleted["input_file"] is False
        assert result.deleted["output_files"] == 0
        _s3(aws_region).head_object(Bucket=INPUT_BUCKET, Key="invoice.pdf")
        assert "Item" in table.get_item(Key={"PK": "doc#invoice.pdf", "SK": "none"})

    def test_a_document_with_no_tracking_record_still_deletes_its_objects(
        self, idp_stack, aws_region
    ):
        """Deleting a document the tracking table never recorded must still
        reclaim the S3 bytes; reporting ``list_entries: False`` is the honest
        answer for the part that was not there."""
        _s3(aws_region).put_object(Bucket=INPUT_BUCKET, Key="orphan.pdf", Body=b"%PDF")

        result = idp_stack.document.delete("orphan.pdf")

        assert result.success is True
        assert result.deleted["input_file"] is True
        assert result.deleted["list_entries"] is False
        assert result.deleted["run_records"] == 0

    def test_a_stack_missing_a_required_resource_is_refused(
        self, aws_credentials, aws_region, monkeypatch
    ):
        """``delete`` needs all three of InputBucket, OutputBucket and
        DocumentsTable. ``StackInfo`` returns ``""`` for an output the stack does
        not declare, so the guard has to be a truthiness check rather than a
        key check — and the error must name what is missing."""
        with mock_aws():
            template = json.loads(json.dumps(STACK_TEMPLATE))
            del template["Outputs"]["S3OutputBucketName"]
            cfn = boto3.client("cloudformation", region_name=aws_region)
            cfn.create_stack(StackName=STACK_NAME, TemplateBody=json.dumps(template))
            client = IDPClient(stack_name=STACK_NAME, region=aws_region)

            with pytest.raises(IDPResourceNotFoundError, match="OutputBucket"):
                client.document.delete("invoice.pdf")

    def test_a_deletion_failure_is_wrapped(self, idp_stack, monkeypatch):
        monkeypatch.setattr(
            "idp_common.delete_documents.delete_documents",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("table throttled")),
        )

        with pytest.raises(IDPProcessingError, match="table throttled"):
            idp_stack.document.delete("invoice.pdf")

    def test_an_empty_results_list_is_reported_as_a_failure(
        self, idp_stack, monkeypatch
    ):
        """``delete_documents`` returning no per-document result is not a
        success. Falling through to ``success=True`` would tell a caller their
        document was deleted when nothing was attempted."""
        monkeypatch.setattr(
            "idp_common.delete_documents.delete_documents",
            lambda **kwargs: {"success": True, "results": []},
        )

        result = idp_stack.document.delete("invoice.pdf")

        assert result.success is False
        assert result.errors == ["No deletion result returned"]
        assert result.object_key == "invoice.pdf"


# ---------------------------------------------------------------------------
# get_metadata()
# ---------------------------------------------------------------------------

#: What ``DocumentProcessor.get_metadata`` returns for one extracted section.
METADATA_RESULT = {
    "document_id": "batch-1/invoice.pdf",
    "section_id": 2,
    "document_class": "Invoice",
    "fields": {"total_amount": "1042.55", "vendor": "Acme Corp"},
    "confidence": {"total_amount": 0.97, "vendor": 0.42},
    "page_count": 4,
    "metadata": {"extraction_model": "us.anthropic.claude-sonnet-4-20250514-v1:0"},
}


def _patch_document_processor(monkeypatch, return_value=None, raises=None):
    calls = {}

    class FakeDocumentProcessor:
        def __init__(self, stack_name, region=None, **kwargs):
            calls["init"] = {"stack_name": stack_name, "region": region}

        def get_metadata(self, document_id, section_id):
            calls["get_metadata"] = {
                "document_id": document_id,
                "section_id": section_id,
            }
            if raises is not None:
                raise raises
            return return_value

    monkeypatch.setattr(
        "idp_sdk._core.document_processor.DocumentProcessor", FakeDocumentProcessor
    )
    return calls


class TestGetMetadata:
    def test_every_field_is_carried_across(self, aws_credentials, monkeypatch):
        """Field-by-field, because the model accepts unknown keyword arguments
        silently: a mis-spelled mapping produces a result whose fields are empty
        rather than an error. (That is the defect
        ``test_document_list_operation.py`` documents for ``list``.)"""
        calls = _patch_document_processor(monkeypatch, return_value=METADATA_RESULT)

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        meta = client.document.get_metadata("batch-1/invoice.pdf", section_id=2)

        assert isinstance(meta, DocumentMetadata)
        assert meta.document_id == "batch-1/invoice.pdf"
        assert meta.section_id == 2
        assert meta.document_class == "Invoice"
        assert meta.fields == {"total_amount": "1042.55", "vendor": "Acme Corp"}
        assert meta.confidence == {"total_amount": 0.97, "vendor": 0.42}
        assert meta.page_count == 4
        assert meta.metadata is not None
        assert "claude-sonnet-4" in meta.metadata["extraction_model"]
        assert calls["get_metadata"] == {
            "document_id": "batch-1/invoice.pdf",
            "section_id": 2,
        }

    def test_section_one_is_the_default(self, aws_credentials, monkeypatch):
        calls = _patch_document_processor(monkeypatch, return_value=METADATA_RESULT)

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        client.document.get_metadata("batch-1/invoice.pdf")

        assert calls["get_metadata"]["section_id"] == 1

    def test_a_missing_section_is_a_resource_not_found_error(
        self, aws_credentials, monkeypatch
    ):
        """The processor signals "no such result object" with
        ``FileNotFoundError``; collapsing that into ``IDPProcessingError`` would
        stop callers distinguishing "not processed yet" from "broken"."""
        _patch_document_processor(
            monkeypatch, raises=FileNotFoundError("Results not found for section 9")
        )

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        with pytest.raises(IDPResourceNotFoundError, match="section 9"):
            client.document.get_metadata("batch-1/invoice.pdf", section_id=9)

    def test_any_other_failure_is_a_processing_error(
        self, aws_credentials, monkeypatch
    ):
        _patch_document_processor(monkeypatch, raises=ValueError("bad JSON"))

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        with pytest.raises(IDPProcessingError, match="bad JSON"):
            client.document.get_metadata("batch-1/invoice.pdf")

    def test_a_stack_override_reaches_the_processor(self, aws_credentials, monkeypatch):
        """``stack_name=`` is the documented multi-deployment escape hatch, and it
        is the argument the processor is constructed with."""
        calls = _patch_document_processor(monkeypatch, return_value=METADATA_RESULT)

        client = IDPClient(stack_name=STACK_NAME, region="us-east-1")
        client.document.get_metadata("invoice.pdf", stack_name="other-stack")

        assert calls["init"]["stack_name"] == "other-stack"
