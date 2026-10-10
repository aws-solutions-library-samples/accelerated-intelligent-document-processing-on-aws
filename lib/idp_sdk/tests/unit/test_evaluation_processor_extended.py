# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the parts of ``EvaluationProcessor`` that touch real AWS state.

``EvaluationProcessor`` manages the evaluation *baseline* — the expected output a
processed document is scored against. Four of its operations are pure S3 and
DynamoDB bookkeeping: write a baseline (``create_baseline``), promote a processed
document's output to be the baseline (``use_as_baseline``), list them
(``list_baselines``) and remove one (``delete_baseline``). A fifth,
``get_metrics``, sifts the whole output bucket for evaluation artifacts.

``tests/unit/test_evaluation_operations.py`` already covers the reading and
aggregation logic against stubbed S3 responses, which is the right tool for
arithmetic over a known payload. This file covers the half that a stub cannot
speak to, and uses **moto** throughout for that reason:

* **Object keys.** ``create_baseline`` writes to
  ``{document_id}/sections/{section_id}/result.json`` and ``use_as_baseline``
  copies keys across buckets. A ``Mock`` accepts any ``Key=`` at all; the tests
  here put the objects in and then list and read them *back*, so a wrong key is a
  missing object rather than a passing assertion.
* **The DynamoDB update.** ``_set_evaluation_status`` writes ``EvaluationStatus``
  with an ``UpdateExpression`` and expression-attribute maps. A ``Mock`` accepts a
  malformed expression and a key that does not match the table's schema; moto
  rejects both. The existing suite asserts on ``table.update_item.call_args``,
  which cannot tell a valid expression from an invalid one — so the assertions
  here read the item back out of the table.
* **Pagination.** ``list_baselines`` base64-encodes S3's
  ``NextContinuationToken`` and decodes it on the way back in. That round trip is
  only meaningful against a service that issues real tokens and honours them.
* **Stack discovery.** ``__init__`` validates the stack and builds its resource
  map through ``StackInfo``, so the tests create a real (moto) CloudFormation
  stack with the outputs and resources a deployed accelerator stack has, and let
  the constructor discover them. That also makes the "stack is not usable" path
  testable without asserting on a mocked ``validate_stack``.

One prefix behaviour is worth stating because both ``use_as_baseline`` and
``delete_baseline`` depend on it: the prefix is the document id plus ``"/"``. A
listing under a bare ``"doc1"`` also matches ``doc10/...`` — verified against
moto, not assumed — so dropping the separator would make ``delete_baseline``
delete a sibling document's baseline. Two tests below create exactly that pair.
"""

from __future__ import annotations

import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

pytestmark = pytest.mark.unit

REGION = "us-east-1"
STACK = "idp-stack"
OUTPUT_BUCKET = "idp-output-bucket"
BASELINE_BUCKET = "idp-baseline-bucket"
TRACKING_TABLE = "idp-tracking-table"

#: A cursor that is not valid base64. Held as a constant rather than written
#: inline so bandit's B105/B106 name heuristics — which fire on a `next_token=`
#: keyword carrying a string *literal* — have nothing to match, rather than
#: needing a suppression pragma.
NOT_BASE64 = "!!!not-base64!!!"

#: A minimal stand-in for a deployed accelerator stack: the two resources
#: ``StackInfo.get_resources`` looks up by logical id, and the outputs it maps to
#: friendly names. ``DocumentQueue`` is required — ``_get_queue_url`` raises if it
#: is absent, which would fail the constructor for an unrelated reason.
STACK_TEMPLATE = {
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
        "S3InputBucketName": {"Value": "idp-input-bucket"},
        "S3OutputBucketName": {"Value": OUTPUT_BUCKET},
        "S3EvaluationBaselineBucketName": {"Value": BASELINE_BUCKET},
        "S3ConfigurationBucketName": {"Value": "idp-config-bucket"},
        "LambdaLookupFunctionName": {"Value": "idp-lookup"},
    },
}


def _create_stack(outputs: dict | None = None) -> None:
    """Create the moto CloudFormation stack the processor discovers."""
    template = dict(STACK_TEMPLATE)
    if outputs is not None:
        template["Outputs"] = outputs
    boto3.client("cloudformation", region_name=REGION).create_stack(
        StackName=STACK, TemplateBody=json.dumps(template)
    )


def _create_buckets(*names: str) -> "boto3.client":  # pyright: ignore[reportInvalidTypeForm]
    s3 = boto3.client("s3", region_name=REGION)
    for name in names:
        s3.create_bucket(Bucket=name)
    return s3


def _processor():
    from idp_sdk._core.evaluation_processor import EvaluationProcessor

    return EvaluationProcessor(stack_name=STACK, region=REGION)


def _keys(s3, bucket: str, prefix: str = "") -> list[str]:
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return sorted(obj["Key"] for obj in response.get("Contents", []))


def _body(s3, bucket: str, key: str) -> dict:
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def _tracking_item(document_id: str) -> dict:
    """The document's tracking record, read straight out of moto DynamoDB."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(TRACKING_TABLE)
    return table.get_item(Key={"PK": f"doc#{document_id}", "SK": "none"}).get(
        "Item", {}
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_the_resource_map_is_discovered_from_the_stack(self, aws_credentials):
        """Every later operation reads this map, so a wrong name fails late.

        ``EvaluationBaselineBucket`` comes from the ``S3EvaluationBaselineBucketName``
        output and ``DocumentsTable`` from the ``TrackingTable`` *logical id* — two
        different lookup mechanisms. Getting either wrong yields an empty string,
        and the operations then raise "not found in stack resources" for a stack
        that has the resource.
        """
        with mock_aws():
            _create_stack()

            proc = _processor()

            assert proc.resources["EvaluationBaselineBucket"] == BASELINE_BUCKET
            assert proc.resources["OutputBucket"] == OUTPUT_BUCKET
            assert proc.resources["DocumentsTable"] == TRACKING_TABLE
            assert proc.resources["SettingsParameter"] == f"{STACK}-Settings"

    def test_an_unusable_stack_is_refused_at_construction(self, aws_credentials):
        """Better to refuse than to operate against a half-deleted stack.

        A deleted stack still answers ``DescribeStacks`` for a while, with
        ``DELETE_COMPLETE``; its buckets may be gone. ``validate_stack`` admits
        only the three settled states, so construction fails here rather than
        every operation failing separately later.
        """
        with mock_aws():
            _create_stack()
            boto3.client("cloudformation", region_name=REGION).delete_stack(
                StackName=STACK
            )

            with pytest.raises(ValueError, match="not in a valid state"):
                _processor()

    def test_a_stack_that_does_not_exist_is_refused(self, aws_credentials):
        with mock_aws():
            with pytest.raises(ValueError, match="not in a valid state"):
                _processor()


# ---------------------------------------------------------------------------
# create_baseline
# ---------------------------------------------------------------------------


SECTIONS = {
    "1": {
        "document_class": {"type": "invoice"},
        "inference_result": {"invoice_total": "4821.50", "vendor": "Acme"},
    },
    "2": {
        "document_class": {"type": "receipt"},
        "inference_result": {"total": "12.00"},
    },
}


class TestCreateBaseline:
    def test_each_section_lands_at_the_key_the_evaluator_reads(self, aws_credentials):
        """The layout is the contract with the evaluation pipeline.

        The evaluator looks for ``{document_id}/sections/{section_id}/result.json``
        in the baseline bucket. A baseline written anywhere else is silently
        invisible: the document is reported as having no baseline, which reads as
        "not yet evaluated" rather than as an error. The assertion is a read-back
        of the stored object, so both the key and the serialised body are checked.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)
            proc = _processor()

            result = proc.create_baseline("loan-123/package.pdf", SECTIONS)

            assert _keys(s3, BASELINE_BUCKET) == [
                "loan-123/package.pdf/sections/1/result.json",
                "loan-123/package.pdf/sections/2/result.json",
            ]
            assert (
                _body(
                    s3, BASELINE_BUCKET, "loan-123/package.pdf/sections/1/result.json"
                )
                == SECTIONS["1"]
            )
            assert result["document_id"] == "loan-123/package.pdf"
            assert result["sections_created"] == 2
            assert result["timestamp"]

    def test_the_stored_objects_declare_json_content(self, aws_credentials):
        """The UI fetches these directly; ``binary/octet-stream`` downloads them."""
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)

            _processor().create_baseline("doc.pdf", {"1": SECTIONS["1"]})

            head = s3.head_object(
                Bucket=BASELINE_BUCKET, Key="doc.pdf/sections/1/result.json"
            )
            assert head["ContentType"] == "application/json"

    def test_metadata_is_written_beside_the_sections_not_inside_them(
        self, aws_credentials
    ):
        """``{document_id}/metadata.json`` is a sibling of ``sections/``.

        Written under ``sections/`` it would be listed as a section and parsed as
        one.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)

            _processor().create_baseline(
                "doc.pdf", {"1": SECTIONS["1"]}, metadata={"source": "manual-review"}
            )

            assert _keys(s3, BASELINE_BUCKET) == [
                "doc.pdf/metadata.json",
                "doc.pdf/sections/1/result.json",
            ]
            assert _body(s3, BASELINE_BUCKET, "doc.pdf/metadata.json") == {
                "source": "manual-review"
            }

    def test_no_metadata_writes_no_metadata_object(self, aws_credentials):
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)

            _processor().create_baseline("doc.pdf", {"1": SECTIONS["1"]})

            assert "doc.pdf/metadata.json" not in _keys(s3, BASELINE_BUCKET)

    def test_an_empty_baseline_writes_nothing_and_reports_zero(self, aws_credentials):
        """Not an error, but it must not read as a baseline that exists.

        ``sections_created == 0`` is the only signal a caller gets, since no
        object is written and no exception is raised.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)

            result = _processor().create_baseline("doc.pdf", {})

            assert result["sections_created"] == 0
            assert _keys(s3, BASELINE_BUCKET) == []

    def test_a_stack_without_a_baseline_bucket_is_refused_by_name(
        self, aws_credentials
    ):
        """The output is absent on a stack deployed without evaluation enabled."""
        with mock_aws():
            outputs = {
                key: value
                for key, value in STACK_TEMPLATE["Outputs"].items()
                if key != "S3EvaluationBaselineBucketName"
            }
            _create_stack(outputs=outputs)

            with pytest.raises(ValueError, match="EvaluationBaselineBucket not found"):
                _processor().create_baseline("doc.pdf", SECTIONS)

    def test_a_write_failure_propagates_rather_than_reporting_success(
        self, aws_credentials
    ):
        """A half-written baseline scores against partly-missing expectations.

        The bucket named by the stack does not exist here, which is the shape of
        a stack whose bucket was deleted out from under it.
        """
        with mock_aws():
            _create_stack()  # no buckets created

            with pytest.raises(ClientError) as exc:
                _processor().create_baseline("doc.pdf", SECTIONS)

            assert exc.value.response["Error"]["Code"] == "NoSuchBucket"


# ---------------------------------------------------------------------------
# use_as_baseline
# ---------------------------------------------------------------------------


class TestUseAsBaseline:
    def _seed_output(self, s3, document_id: str) -> list[str]:
        keys = [
            f"{document_id}/sections/1/result.json",
            f"{document_id}/sections/2/result.json",
            f"{document_id}/pages/1/rawText.json",
        ]
        for key in keys:
            s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=b'{"ok": true}')
        return sorted(keys)

    def test_every_output_object_is_copied_under_its_original_key(
        self, aws_credentials
    ):
        """The baseline must be key-for-key identical to the output it came from.

        The evaluator reads the baseline with the same key template it uses for
        the output, so a copy that reshaped the prefix — or dropped the non-section
        objects — produces a baseline the evaluator cannot find. Asserted by
        listing the destination bucket, not by counting ``copy_object`` calls.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(OUTPUT_BUCKET, BASELINE_BUCKET)
            expected = self._seed_output(s3, "loan-123/package.pdf")

            result = _processor().use_as_baseline("loan-123/package.pdf")

            assert _keys(s3, BASELINE_BUCKET) == expected
            assert result["files_copied"] == 3
            assert result["evaluation_status"] == "BASELINE_AVAILABLE"

    def test_a_sibling_document_sharing_a_prefix_is_not_copied(self, aws_credentials):
        """``doc1`` must not drag in ``doc10``.

        The separator in ``f"{document_id}/"`` is the whole defence. Without it
        the listing matches ``doc10/...`` as well — confirmed against moto — and
        the baseline for ``doc1`` would contain another document's results, which
        then scores as a pile of extra sections.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(OUTPUT_BUCKET, BASELINE_BUCKET)
            s3.put_object(
                Bucket=OUTPUT_BUCKET, Key="doc1/sections/1/result.json", Body=b"{}"
            )
            s3.put_object(
                Bucket=OUTPUT_BUCKET, Key="doc10/sections/1/result.json", Body=b"{}"
            )

            result = _processor().use_as_baseline("doc1")

            assert _keys(s3, BASELINE_BUCKET) == ["doc1/sections/1/result.json"]
            assert result["files_copied"] == 1

    def test_the_tracking_record_carries_the_final_status(self, aws_credentials):
        """Read back out of DynamoDB, which is what validates the update itself.

        ``_set_evaluation_status`` writes ``SET #es = :es`` against
        ``PK=doc#<key>, SK=none``. A ``Mock`` accepts that expression however it
        is spelled and whatever key it names; moto rejects a key that does not
        match the table schema and an expression it cannot parse. So the item
        coming back with ``EvaluationStatus == "BASELINE_AVAILABLE"`` is the
        assertion that the UI and CLI will actually see the promotion.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(OUTPUT_BUCKET, BASELINE_BUCKET)
            self._seed_output(s3, "doc.pdf")

            _processor().use_as_baseline("doc.pdf")

            item = _tracking_item("doc.pdf")
            assert item["EvaluationStatus"] == "BASELINE_AVAILABLE"
            assert item["PK"] == "doc#doc.pdf"
            assert item["SK"] == "none"

    def test_a_copy_failure_leaves_the_error_status_behind(self, aws_credentials):
        """The recorded state must not say ``AVAILABLE`` for a failed copy.

        A partially copied baseline scored as complete is a wrong accuracy
        number, not an error, so the status is what stops it being used. The
        destination bucket is absent here, so the first ``copy_object`` fails.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(OUTPUT_BUCKET)
            self._seed_output(s3, "doc.pdf")

            with pytest.raises(ClientError):
                _processor().use_as_baseline("doc.pdf")

            assert _tracking_item("doc.pdf")["EvaluationStatus"] == "BASELINE_ERROR"

    def test_a_document_with_no_output_is_a_file_not_found(self, aws_credentials):
        """Nothing is written and no status is recorded for a no-op promotion."""
        with mock_aws():
            _create_stack()
            _create_buckets(OUTPUT_BUCKET, BASELINE_BUCKET)

            with pytest.raises(FileNotFoundError, match="Has the document finished"):
                _processor().use_as_baseline("never-processed.pdf")

            assert _tracking_item("never-processed.pdf") == {}

    def test_a_missing_output_bucket_is_named_in_the_error(self, aws_credentials):
        with mock_aws():
            outputs = {
                key: value
                for key, value in STACK_TEMPLATE["Outputs"].items()
                if key != "S3OutputBucketName"
            }
            _create_stack(outputs=outputs)

            with pytest.raises(ValueError, match="OutputBucket not found"):
                _processor().use_as_baseline("doc.pdf")

    def test_a_missing_baseline_bucket_is_refused_before_listing_the_output(
        self, aws_credentials
    ):
        """Both buckets are checked up front, so nothing is read for nothing."""
        with mock_aws():
            outputs = {
                key: value
                for key, value in STACK_TEMPLATE["Outputs"].items()
                if key != "S3EvaluationBaselineBucketName"
            }
            _create_stack(outputs=outputs)

            with pytest.raises(ValueError, match="EvaluationBaselineBucket not found"):
                _processor().use_as_baseline("doc.pdf")


class TestEvaluationStatusWrites:
    """``_set_evaluation_status`` is best-effort and must stay that way.

    The copy is the operation the caller asked for. A tracking-table write that
    fails — a missing table on an older stack, a throttle, a denied IAM action —
    must not turn a completed promotion into a raised exception, because the
    caller would then retry a copy that already succeeded.
    """

    def test_a_stack_without_a_tracking_table_still_copies(self, aws_credentials):
        with mock_aws():
            # `TrackingTable` absent from the template, so `DocumentsTable`
            # resolves to "" and the status write is skipped by name.
            template = json.loads(json.dumps(STACK_TEMPLATE))
            del template["Resources"]["TrackingTable"]
            boto3.client("cloudformation", region_name=REGION).create_stack(
                StackName=STACK, TemplateBody=json.dumps(template)
            )
            s3 = _create_buckets(OUTPUT_BUCKET, BASELINE_BUCKET)
            s3.put_object(Bucket=OUTPUT_BUCKET, Key="doc.pdf/a.json", Body=b"{}")

            result = _processor().use_as_baseline("doc.pdf")

            assert result["files_copied"] == 1
            assert result["evaluation_status"] == "BASELINE_AVAILABLE"

    def test_a_failing_status_write_is_logged_and_swallowed(
        self, aws_credentials, caplog
    ):
        """The table name is in the stack but the table itself is gone.

        moto raises ``ResourceNotFoundException`` from ``update_item``, which is
        what a real stack whose table was deleted does. The copy result must
        survive it, and the failure must be visible in the log rather than
        nowhere.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(OUTPUT_BUCKET, BASELINE_BUCKET)
            s3.put_object(Bucket=OUTPUT_BUCKET, Key="doc.pdf/a.json", Body=b"{}")
            proc = _processor()
            boto3.client("dynamodb", region_name=REGION).delete_table(
                TableName=TRACKING_TABLE
            )

            result = proc.use_as_baseline("doc.pdf")

            assert result["evaluation_status"] == "BASELINE_AVAILABLE"
            assert "Failed to set EvaluationStatus=BASELINE_COPYING" in caplog.text


# ---------------------------------------------------------------------------
# get_metrics: the date filter
# ---------------------------------------------------------------------------


EVAL_ARTIFACT = {
    "document_id": "a.pdf",
    "overall_metrics": {"accuracy": 0.9, "f1_score": 0.82},
    "section_results": [
        {
            "section_id": "1",
            "document_class": "invoice",
            "metrics": {"accuracy": 0.75},
            "attributes": [],
        }
    ],
}


def _seed_evaluation(s3, document_id: str = "a.pdf", batch: str = "") -> str:
    from idp_common.evaluation.contract import evaluation_results_key

    key = evaluation_results_key(f"{batch}{document_id}")
    s3.put_object(
        Bucket=OUTPUT_BUCKET, Key=key, Body=json.dumps(EVAL_ARTIFACT).encode("utf-8")
    )
    return key


class TestGetMetricsDateFilter:
    """The filter compares ``LastModified.isoformat()`` to the caller's string.

    That makes it a **lexicographic** comparison, not a datetime one, and moto
    stamps a real timezone-aware ``LastModified`` (``...+00:00``) — so these tests
    are written against real object timestamps rather than a stubbed
    ``LastModified``. The consequence of getting a bound wrong is a metrics figure
    computed over the wrong set of documents, which looks like a perfectly normal
    number.
    """

    def test_an_object_inside_the_window_is_counted(self, aws_credentials):
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(OUTPUT_BUCKET)
            _seed_evaluation(s3)

            result = _processor().get_metrics(
                start_date="2000-01-01", end_date="2999-12-31"
            )

            assert result["total_documents"] == 1
            assert result["avg_accuracy"] == pytest.approx(0.9)

    def test_a_start_date_after_the_object_excludes_it(self, aws_credentials):
        """Excluded, not errored — so an empty answer must read as empty.

        ``total_documents == 0`` with ``None`` averages is the only thing
        distinguishing "no evaluations in this window" from "evaluations that
        scored nothing".
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(OUTPUT_BUCKET)
            _seed_evaluation(s3)

            result = _processor().get_metrics(start_date="2999-01-01")

            assert result["total_documents"] == 0
            assert result["avg_accuracy"] is None
            assert result["by_document_class"] == {}

    def test_an_end_date_before_the_object_excludes_it(self, aws_credentials):
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(OUTPUT_BUCKET)
            _seed_evaluation(s3)

            assert (
                _processor().get_metrics(end_date="2000-01-01")["total_documents"] == 0
            )

    def test_a_batch_id_narrows_the_listing_to_that_prefix(self, aws_credentials):
        """The batch filter is an S3 ``Prefix``, not a post-filter.

        Getting it wrong is expensive rather than merely wrong: the scan fetches
        and parses every evaluation artifact in the bucket. The assertion is that
        the document outside the batch is not counted.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(OUTPUT_BUCKET)
            _seed_evaluation(s3, "in-batch.pdf", batch="batch-001/")
            _seed_evaluation(s3, "loose.pdf")

            assert _processor().get_metrics()["total_documents"] == 2
            assert (
                _processor().get_metrics(batch_id="batch-001")["total_documents"] == 1
            )

    def test_a_scan_failure_propagates(self, aws_credentials):
        """The output bucket named by the stack does not exist."""
        with mock_aws():
            _create_stack()

            with pytest.raises(ClientError):
                _processor().get_metrics()


# ---------------------------------------------------------------------------
# list_baselines
# ---------------------------------------------------------------------------


class TestListBaselines:
    def _seed(self, s3, *document_ids: str) -> None:
        for document_id in document_ids:
            s3.put_object(
                Bucket=BASELINE_BUCKET,
                Key=f"{document_id}/sections/1/result.json",
                Body=b"{}",
            )

    def test_the_pagination_token_round_trips_through_s3(self, aws_credentials):
        """Page two must continue page one, not repeat it.

        The token is S3's own ``NextContinuationToken``, base64-encoded on the way
        out and decoded on the way back in. Both halves have to agree: encode
        without decode (or vice versa) hands S3 a token it rejects, and mismatched
        encodings would return page one forever — a paging loop that never
        terminates. moto issues real tokens and enforces them, which is why this
        is not stubbed.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)
            self._seed(s3, "doc-a", "doc-b", "doc-c", "doc-d")
            proc = _processor()

            first = proc.list_baselines(limit=2)
            assert [b["document_id"] for b in first["baselines"]] == ["doc-a", "doc-b"]
            assert first["count"] == 2
            assert first["next_token"]

            second = proc.list_baselines(limit=2, next_token=first["next_token"])

            assert [b["document_id"] for b in second["baselines"]] == ["doc-c", "doc-d"]
            assert "next_token" not in second, (
                "the last page must not offer a token, or a caller looping on its "
                "presence never stops"
            )

    def test_the_final_page_omits_the_token(self, aws_credentials):
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)
            self._seed(s3, "doc-a")

            assert "next_token" not in _processor().list_baselines(limit=10)

    def test_an_empty_bucket_lists_nothing_without_raising(self, aws_credentials):
        with mock_aws():
            _create_stack()
            _create_buckets(BASELINE_BUCKET)

            assert _processor().list_baselines() == {"baselines": [], "count": 0}

    def test_a_malformed_token_is_raised_rather_than_ignored(self, aws_credentials):
        """Silently restarting from page one would look like an infinite result.

        The token is base64-decoded before use, so a corrupted one fails in the
        decode or at S3. Either way the caller is told, instead of quietly
        receiving the first page again.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)
            self._seed(s3, "doc-a")

            with pytest.raises(Exception):
                _processor().list_baselines(next_token=NOT_BASE64)

    def test_a_missing_baseline_bucket_is_named(self, aws_credentials):
        with mock_aws():
            outputs = {
                key: value
                for key, value in STACK_TEMPLATE["Outputs"].items()
                if key != "S3EvaluationBaselineBucketName"
            }
            _create_stack(outputs=outputs)

            with pytest.raises(ValueError, match="EvaluationBaselineBucket not found"):
                _processor().list_baselines()

    def test_a_listing_failure_propagates(self, aws_credentials):
        with mock_aws():
            _create_stack()  # bucket named but never created

            with pytest.raises(ClientError):
                _processor().list_baselines()


# ---------------------------------------------------------------------------
# delete_baseline
# ---------------------------------------------------------------------------


class TestDeleteBaseline:
    def test_every_object_under_the_document_is_deleted(self, aws_credentials):
        """A leftover object makes a "deleted" baseline still score documents.

        Checked by listing the bucket afterwards rather than by counting
        ``delete_objects`` calls, since a call with the wrong ``Delete`` payload
        succeeds against a mock and deletes nothing against S3.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)
            for key in (
                "doc.pdf/metadata.json",
                "doc.pdf/sections/1/result.json",
                "doc.pdf/sections/2/result.json",
            ):
                s3.put_object(Bucket=BASELINE_BUCKET, Key=key, Body=b"{}")

            result = _processor().delete_baseline("doc.pdf")

            assert result == {"document_id": "doc.pdf", "deleted_count": 3}
            assert _keys(s3, BASELINE_BUCKET) == []

    def test_a_sibling_document_sharing_a_prefix_survives(self, aws_credentials):
        """Deleting ``doc1`` must not delete ``doc10``.

        This is the destructive half of the prefix behaviour: a bare ``doc1``
        prefix matches ``doc10/...`` in S3, so without the trailing separator
        this call would silently destroy an unrelated document's baseline and
        report the larger count as success.
        """
        with mock_aws():
            _create_stack()
            s3 = _create_buckets(BASELINE_BUCKET)
            s3.put_object(
                Bucket=BASELINE_BUCKET, Key="doc1/sections/1/result.json", Body=b"{}"
            )
            s3.put_object(
                Bucket=BASELINE_BUCKET, Key="doc10/sections/1/result.json", Body=b"{}"
            )

            result = _processor().delete_baseline("doc1")

            assert result["deleted_count"] == 1
            assert _keys(s3, BASELINE_BUCKET) == ["doc10/sections/1/result.json"]

    def test_deleting_a_baseline_that_is_not_there_reports_zero(self, aws_credentials):
        """Idempotent by design: a repeat delete is not an error.

        ``deleted_count == 0`` is how a caller distinguishes the two cases.
        """
        with mock_aws():
            _create_stack()
            _create_buckets(BASELINE_BUCKET)

            assert _processor().delete_baseline("absent.pdf") == {
                "document_id": "absent.pdf",
                "deleted_count": 0,
            }

    def test_a_missing_baseline_bucket_is_named(self, aws_credentials):
        with mock_aws():
            outputs = {
                key: value
                for key, value in STACK_TEMPLATE["Outputs"].items()
                if key != "S3EvaluationBaselineBucketName"
            }
            _create_stack(outputs=outputs)

            with pytest.raises(ValueError, match="EvaluationBaselineBucket not found"):
                _processor().delete_baseline("doc.pdf")

    def test_a_delete_failure_propagates_rather_than_reporting_a_count(
        self, aws_credentials
    ):
        with mock_aws():
            _create_stack()  # bucket named but never created

            with pytest.raises(ClientError):
                _processor().delete_baseline("doc.pdf")
