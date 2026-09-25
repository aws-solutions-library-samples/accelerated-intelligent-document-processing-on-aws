# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``DocumentProcessor``'s two AWS-backed operations.

``DocumentProcessor`` is the read side of the SDK: ``get_metadata`` reads one
section's extraction result out of the output bucket and reshapes it, and
``list_documents`` pages the DynamoDB tracking table. Both are thin, and that is
the point — almost everything they can get wrong is a *location* (which S3 key,
which table) or a *reshaping* (which output field comes from which input field),
and neither shows up as an exception. A wrong key is "results not found" for a
document that processed fine; a wrong field mapping is a populated result with a
blank column in it.

Two existing files cover the neighbouring ground and are not duplicated here.
``tests/unit/test_multi_instance_sdk.py`` covers the two module-level helpers
(``_section_instances`` and ``_collect_confidence``) directly and exhaustively, so
the confidence assertions below only check that ``get_metadata`` actually *calls*
the walker and puts the result where callers read it — the flattening rules
themselves are that file's subject. ``tests/unit/test_document_list_operation.py``
covers the operation layer above ``list_documents``, against a stubbed processor.

Everything here runs against **moto**, for both operations and for construction:

* the section result is ``put_object``-ed at a literal key and then fetched
  through ``get_metadata``, so the key template is exercised in the direction that
  can fail rather than asserted as a string;
* the tracking table is a real (fake) DynamoDB table with the accelerator's
  ``PK``/``SK`` schema, so the pagination round trip — ``LastEvaluatedKey`` out,
  base64 ``next_token`` back in as ``ExclusiveStartKey`` — is enforced by
  something that rejects a malformed key, which a ``Mock`` does not;
* ``__init__`` discovers its resources from a real moto CloudFormation stack, so
  "which output feeds which resource name" is checked end to end.
"""

from __future__ import annotations

import json

import boto3
import pytest
from botocore.exceptions import ClientError, ParamValidationError
from moto import mock_aws

pytestmark = pytest.mark.unit

REGION = "us-east-1"
STACK = "idp-stack"
OUTPUT_BUCKET = "idp-output-bucket"
TRACKING_TABLE = "idp-tracking-table"

#: A cursor that is not valid base64. Held as a constant rather than written
#: inline so bandit's B105/B106 name heuristics — which fire on a `next_token=`
#: keyword carrying a string *literal* — have nothing to match, rather than
#: needing a suppression pragma.
NOT_BASE64 = "!!!not-base64!!!"

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
        "LambdaLookupFunctionName": {"Value": "idp-lookup"},
    },
}


def _create_stack(*, drop_output: bool = False, drop_table: bool = False) -> None:
    template = json.loads(json.dumps(STACK_TEMPLATE))
    if drop_output:
        del template["Outputs"]["S3OutputBucketName"]
    if drop_table:
        del template["Resources"]["TrackingTable"]
    boto3.client("cloudformation", region_name=REGION).create_stack(
        StackName=STACK, TemplateBody=json.dumps(template)
    )


def _processor():
    from idp_sdk._core.document_processor import DocumentProcessor

    return DocumentProcessor(stack_name=STACK, region=REGION)


def _put_section(document_id: str, section_id: int, payload: dict) -> str:
    s3 = boto3.client("s3", region_name=REGION)
    key = f"{document_id}/sections/{section_id}/result.json"
    s3.put_object(
        Bucket=OUTPUT_BUCKET, Key=key, Body=json.dumps(payload).encode("utf-8")
    )
    return key


def _leaf(confidence):
    """One ``explainability_info`` leaf, as the extraction service writes it."""
    return {"confidence": confidence, "confidence_threshold": 0.8}


#: One section's ``result.json``, in the shape the extraction step writes.
SECTION_RESULT = {
    "document_class": {"type": "invoice", "confidence": 0.99},
    "inference_result": {"invoice_total": "4821.50", "vendor": "Acme Supply"},
    "split_document": {"page_indices": [0, 1, 2]},
    "explainability_info": [
        {"invoice_total": _leaf(0.94), "vendor": _leaf(0.88)},
    ],
}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_resources_are_discovered_from_the_stack(self, aws_credentials):
        with mock_aws():
            _create_stack()

            proc = _processor()

            assert proc.resources["OutputBucket"] == OUTPUT_BUCKET
            assert proc.resources["DocumentsTable"] == TRACKING_TABLE

    def test_an_unusable_stack_is_refused_at_construction(self, aws_credentials):
        """Refused once, rather than failing differently in each operation."""
        with mock_aws():
            _create_stack()
            boto3.client("cloudformation", region_name=REGION).delete_stack(
                StackName=STACK
            )

            with pytest.raises(ValueError, match="not in a valid state"):
                _processor()


# ---------------------------------------------------------------------------
# get_metadata
# ---------------------------------------------------------------------------


class TestGetMetadata:
    def test_a_section_result_is_read_from_the_pipelines_key(self, aws_credentials):
        """The whole returned shape, asserted at once.

        Every value here is a *rename*: ``document_class`` comes from the nested
        ``document_class.type`` (not the object), ``fields`` is the raw
        ``inference_result``, ``page_count`` is the length of
        ``split_document.page_indices`` rather than a stored number, and the
        class-level confidence moves into a ``metadata`` sub-dict. Each is a
        silent blank if it drifts, so the comparison is against the full dict
        rather than field by field.
        """
        with mock_aws():
            _create_stack()
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=OUTPUT_BUCKET)
            _put_section("loan-123/package.pdf", 1, SECTION_RESULT)

            result = _processor().get_metadata("loan-123/package.pdf")

            assert result == {
                "document_id": "loan-123/package.pdf",
                "section_id": 1,
                "document_class": "invoice",
                "fields": {"invoice_total": "4821.50", "vendor": "Acme Supply"},
                "instances": None,
                "confidence": {"invoice_total": 0.94, "vendor": 0.88},
                "page_count": 3,
                "metadata": {
                    "document_class_confidence": 0.99,
                    "page_indices": [0, 1, 2],
                },
            }

    def test_the_section_id_selects_the_section(self, aws_credentials):
        """Section ids are part of the key, so the wrong one reads another section.

        Both sections exist here and differ, so an ignored ``section_id`` returns
        plausible data for the wrong part of the document.
        """
        with mock_aws():
            _create_stack()
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=OUTPUT_BUCKET)
            _put_section("doc.pdf", 1, SECTION_RESULT)
            _put_section(
                "doc.pdf",
                2,
                {
                    "document_class": {"type": "receipt"},
                    "inference_result": {"total": "12.00"},
                    "split_document": {"page_indices": [3]},
                },
            )

            second = _processor().get_metadata("doc.pdf", section_id=2)

            assert second["section_id"] == 2
            assert second["document_class"] == "receipt"
            assert second["fields"] == {"total": "12.00"}
            assert second["page_count"] == 1

    def test_a_section_with_no_confidence_data_reports_none_not_an_empty_map(
        self, aws_credentials
    ):
        """``None`` and ``{}`` mean different things to a caller.

        ``{}`` would read as "scored, and nothing was confident"; ``None`` is
        "assessment did not run for this section", which is the true state when
        ``explainability_info`` is absent.
        """
        with mock_aws():
            _create_stack()
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=OUTPUT_BUCKET)
            _put_section(
                "doc.pdf",
                1,
                {
                    "document_class": {"type": "invoice"},
                    "inference_result": {"a": "1"},
                    "split_document": {"page_indices": [0]},
                },
            )

            assert _processor().get_metadata("doc.pdf")["confidence"] is None

    def test_a_non_object_entry_in_explainability_info_is_skipped(
        self, aws_credentials
    ):
        """One malformed entry must not lose the confidences beside it.

        ``explainability_info`` is a list whose entries the pipeline writes per
        section; a string or ``null`` in it would otherwise abort the walk and
        drop every score, turning a data-quality blip into a blank confidence
        column.
        """
        with mock_aws():
            _create_stack()
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=OUTPUT_BUCKET)
            _put_section(
                "doc.pdf",
                1,
                {
                    "document_class": {"type": "invoice"},
                    "inference_result": {"a": "1"},
                    "split_document": {"page_indices": [0]},
                    "explainability_info": ["junk", None, {"a": _leaf(0.7)}],
                },
            )

            assert _processor().get_metadata("doc.pdf")["confidence"] == {"a": 0.7}

    def test_a_multi_instance_section_exposes_its_records_and_their_confidence(
        self, aws_credentials
    ):
        """``instances`` is the additive surface for a multi-instance class.

        For such a class the whole result sits under one ``instances`` list, so
        ``fields`` is a wrapper rather than the fields themselves and a caller
        needs the list. The confidence keys are equally the point: keyed by
        top-level name only, a multi-instance section's confidence map came back
        completely empty. (The flattening rules themselves are covered in
        ``test_multi_instance_sdk.py``; this checks ``get_metadata`` wires them in.)
        """
        records = [
            {"CheckNumber": "77310468", "NetPay": "4,104.59"},
            {"CheckNumber": "77298351", "NetPay": "4,657.95"},
        ]
        with mock_aws():
            _create_stack()
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=OUTPUT_BUCKET)
            _put_section(
                "payroll.pdf",
                1,
                {
                    "document_class": {"type": "paystub"},
                    "inference_result": {"instances": records},
                    "split_document": {"page_indices": [0, 1]},
                    "explainability_info": [
                        {
                            "instances": [
                                {"NetPay": _leaf(0.91)},
                                {"NetPay": _leaf(0.87)},
                            ]
                        }
                    ],
                },
            )

            result = _processor().get_metadata("payroll.pdf")

            assert result["instances"] == records
            assert result["fields"] == {"instances": records}
            assert result["confidence"] == {
                "instances[0].NetPay": 0.91,
                "instances[1].NetPay": 0.87,
            }

    def test_an_absent_section_becomes_a_file_not_found_naming_both_ids(
        self, aws_credentials
    ):
        """``NoSuchKey`` is the expected outcome for an unprocessed document.

        It is translated because the caller's question was about a document and a
        section, not about an S3 key, and the message has to name both to be
        actionable.
        """
        with mock_aws():
            _create_stack()
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=OUTPUT_BUCKET)

            with pytest.raises(FileNotFoundError, match=r"missing\.pdf.*section: 4"):
                _processor().get_metadata("missing.pdf", section_id=4)

    def test_a_permission_failure_is_not_disguised_as_a_missing_document(
        self, aws_credentials
    ):
        """Only ``NoSuchKey`` is translated; everything else must propagate.

        Reporting ``AccessDenied`` as "results not found" would send an operator
        to look for a document that is there, instead of at the IAM policy. Here
        the bucket the stack names does not exist, so S3 answers ``NoSuchBucket``.
        """
        with mock_aws():
            _create_stack()  # bucket named by the stack, never created

            with pytest.raises(ClientError) as exc:
                _processor().get_metadata("doc.pdf")

            assert exc.value.response["Error"]["Code"] == "NoSuchBucket"

    def test_a_stack_without_an_output_bucket_fails_opaquely(self, aws_credentials):
        """DEFECT, pinned as-is: this one condition has two different messages.

        Every other operation on these processors reads the bucket with
        ``.get()`` and raises ``ValueError("OutputBucket not found in stack
        resources")`` — a sentence naming the stack output to look for.
        ``get_metadata`` (and ``EvaluationProcessor.get_report``) subscript
        ``self.resources["OutputBucket"]`` instead. That never raises
        ``KeyError``, because ``StackInfo.get_resources`` always sets the key,
        defaulting it to ``""`` — so the empty name reaches botocore and the
        operator gets a bucket-name regex instead.

        Observable consequence: on a stack deployed without the output (an older
        template, or a partial deploy) ``client.document.get_metadata`` reports a
        parameter-validation error about ``""`` that names neither the stack nor
        the output. Pinned rather than fixed; fixing it is a production change.
        """
        with mock_aws():
            _create_stack(drop_output=True)

            with pytest.raises(ParamValidationError, match="Invalid bucket name"):
                _processor().get_metadata("doc.pdf")


# ---------------------------------------------------------------------------
# list_documents
# ---------------------------------------------------------------------------


def _seed_tracking(*document_ids: str, **extra) -> None:
    table = boto3.resource("dynamodb", region_name=REGION).Table(TRACKING_TABLE)
    for document_id in document_ids:
        item = {
            "PK": f"doc#{document_id}",
            "SK": "none",
            "object_key": document_id,
            "status": "COMPLETED",
            "timestamp": "2026-04-10T12:00:00Z",
        }
        item.update(extra)
        table.put_item(Item=item)


class TestListDocuments:
    def test_each_item_is_projected_onto_the_sdks_four_fields(self, aws_credentials):
        """The tracking record is wide; the SDK returns four fields from it.

        ``document_id`` comes from the item's ``object_key`` — not from ``PK``,
        which carries a ``doc#`` prefix — so returning the key itself would give
        every document id a prefix no other API accepts. Everything else in the
        record is dropped, which is checked by comparing the whole projected dict.
        """
        with mock_aws():
            _create_stack()
            proc = _processor()
            _seed_tracking(
                "batch-001/invoice1.pdf",
                batch_id="batch-001",
                workflow_execution_arn="arn:aws:states:::execution/x",
            )

            result = proc.list_documents()

            assert result["count"] == 1
            assert result["documents"] == [
                {
                    "document_id": "batch-001/invoice1.pdf",
                    "status": "COMPLETED",
                    "timestamp": "2026-04-10T12:00:00Z",
                    "batch_id": "batch-001",
                }
            ]

    def test_an_adhoc_document_has_no_batch_id_rather_than_an_empty_one(
        self, aws_credentials
    ):
        """``None`` distinguishes "not part of a batch" from "batch named ''".

        ``batch_id`` is read with a bare ``.get()`` for exactly this reason, while
        ``status`` and ``timestamp`` carry string defaults.
        """
        with mock_aws():
            _create_stack()
            proc = _processor()
            _seed_tracking("adhoc/invoice2.pdf")

            document = proc.list_documents()["documents"][0]

            assert document["batch_id"] is None

    def test_a_record_missing_its_fields_gets_the_defaults(self, aws_credentials):
        """A record written by an older pipeline version must not break the list.

        An absent ``status`` reads as ``"UNKNOWN"`` rather than ``None``, because
        the field is typed as a string downstream; the alternative is one
        malformed row failing the whole page.
        """
        with mock_aws():
            _create_stack()
            proc = _processor()
            boto3.resource("dynamodb", region_name=REGION).Table(
                TRACKING_TABLE
            ).put_item(Item={"PK": "doc#bare.pdf", "SK": "none"})

            document = proc.list_documents()["documents"][0]

            assert document == {
                "document_id": "",
                "status": "UNKNOWN",
                "timestamp": "",
                "batch_id": None,
            }

    def test_the_page_token_round_trips_through_dynamodb(self, aws_credentials):
        """Page two must continue page one, and the token must survive encoding.

        ``LastEvaluatedKey`` is JSON-serialised, base64-encoded into
        ``next_token``, and decoded back into ``ExclusiveStartKey``. Both ends
        have to agree — a mismatch either raises at DynamoDB or silently returns
        page one forever, which is a paging loop that never terminates. Scan order
        is not part of DynamoDB's contract, so the assertion is on *disjointness*
        and total coverage rather than on which item lands where.
        """
        with mock_aws():
            _create_stack()
            proc = _processor()
            everything = [f"doc-{index}.pdf" for index in range(5)]
            _seed_tracking(*everything)

            first = proc.list_documents(limit=2)
            assert first["count"] == 2
            assert first["next_token"]

            second = proc.list_documents(limit=2, next_token=first["next_token"])

            page_one = {d["document_id"] for d in first["documents"]}
            page_two = {d["document_id"] for d in second["documents"]}
            assert page_one.isdisjoint(page_two), (
                "the continuation token was not honoured, so the second page "
                "repeats the first"
            )
            assert page_one | page_two <= set(everything)

    def test_the_last_page_offers_no_token(self, aws_credentials):
        """A token on the final page is an unterminated loop for the caller."""
        with mock_aws():
            _create_stack()
            proc = _processor()
            _seed_tracking("only.pdf")

            assert "next_token" not in proc.list_documents(limit=10)

    def test_an_empty_table_is_a_count_of_zero(self, aws_credentials):
        with mock_aws():
            _create_stack()

            assert _processor().list_documents() == {"documents": [], "count": 0}

    def test_a_malformed_token_is_raised_rather_than_ignored(self, aws_credentials):
        """Restarting silently from page one would look like unbounded results."""
        with mock_aws():
            _create_stack()
            proc = _processor()
            _seed_tracking("a.pdf")

            with pytest.raises(Exception):
                proc.list_documents(next_token=NOT_BASE64)

    def test_a_stack_without_a_tracking_table_is_refused_by_name(self, aws_credentials):
        """Unlike ``get_metadata``, this one names the resource it wants.

        ``DocumentsTable`` is read with ``.get()`` and checked, so a stack whose
        tracking table is absent produces a sentence an operator can act on —
        the treatment ``get_metadata`` does not give ``OutputBucket``.
        """
        with mock_aws():
            _create_stack(drop_table=True)

            with pytest.raises(ValueError, match="DocumentsTable not found"):
                _processor().list_documents()

    def test_a_scan_failure_propagates(self, aws_credentials):
        """The table named in the stack has been deleted underneath the SDK."""
        with mock_aws():
            _create_stack()
            proc = _processor()
            boto3.client("dynamodb", region_name=REGION).delete_table(
                TableName=TRACKING_TABLE
            )

            with pytest.raises(ClientError) as exc:
                proc.list_documents()

            assert exc.value.response["Error"]["Code"] == "ResourceNotFoundException"
