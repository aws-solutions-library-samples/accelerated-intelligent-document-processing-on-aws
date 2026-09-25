# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_sdk._core.rerun_processor``.

``RerunProcessor`` re-submits documents that have already been processed, starting
from a chosen pipeline step. For each object key it reads the full ``Document``
back out of the stack's tracking table, *rewinds* it — which step decides how much
is thrown away — writes the rewound document back to the tracking table so the UI
and the progress monitor immediately see ``QUEUED``, and then puts it on the
stack's document queue for the state machine to pick up.

Rewinding is the part with consequences, and the two steps differ in a way a
caller cannot see from the outside:

* ``classification`` clears every page's class, **deletes the section extraction
  results from S3**, and replaces the section list with a single placeholder,
  because the number of sections is about to change;
* ``extraction`` keeps the classes, the sections and their page assignments, and
  only drops each section's extraction result pointer — leaving the S3 objects in
  place to be overwritten.

So the tests are written against real state rather than call records: a moto
CloudFormation stack that really creates the SQS queue and the DynamoDB tracking
table, real tracking-table items read back through ``idp_common``'s own
``DocumentDynamoDBService``, real S3 objects to be deleted or spared, and the real
SQS message body. A ``MagicMock`` in any of those positions would accept a delete
aimed at the wrong bucket, a section list that was emptied rather than replaced,
and an update expression DynamoDB would reject.

Three findings are pinned here as current behaviour, each with a docstring saying
so. The largest is that ``_get_idp_common_path`` cannot succeed in this repository
layout, which makes the whole feature inoperable while reporting every document as
"not found"; the tests that exercise the working path substitute a correct path so
that the rest of the module is still covered.
"""

import json
import os
import pathlib
import sys

import boto3
import pytest
from moto import mock_aws

import idp_common
from idp_common.models import Document, Page, Section, Status
from idp_sdk._core import rerun_processor
from idp_sdk._core.rerun_processor import RerunProcessor, _get_idp_common_path

STACK_NAME = "idp-rerun-stack"
INPUT_BUCKET = "idp-rerun-input"
OUTPUT_BUCKET = "idp-rerun-output"
TRACKING_TABLE = "idp-rerun-tracking"
QUEUE_NAME = "idp-rerun-documents"
APPSYNC_URL = "https://gql.example.test/graphql"

ENV_FILE_OUTPUT = (
    "REACT_APP_USER_POOL_ID=us-east-1_abc\n"
    f"REACT_APP_APPSYNC_GRAPHQL_URL={APPSYNC_URL}\n"
    "REACT_APP_AWS_REGION=us-east-1\n"
)

_TABLE_RESOURCE = {
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
}

_QUEUE_RESOURCE = {
    "Type": "AWS::SQS::Queue",
    "Properties": {"QueueName": QUEUE_NAME},
}

DEFAULT_OUTPUTS = {
    "S3InputBucketName": INPUT_BUCKET,
    "S3OutputBucketName": OUTPUT_BUCKET,
    "LambdaLookupFunctionName": "idp-rerun-Lookup",
    "WebUITestEnvFile": ENV_FILE_OUTPUT,
}


def _template(with_table: bool = True, outputs: dict = None) -> str:
    resources = {"DocumentQueue": dict(_QUEUE_RESOURCE)}
    if with_table:
        resources["TrackingTable"] = dict(_TABLE_RESOURCE)
    return json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": resources,
            "Outputs": {
                key: {"Value": value}
                for key, value in (
                    DEFAULT_OUTPUTS if outputs is None else outputs
                ).items()
            },
        }
    )


def _tracking_item(object_key: str = "lending_package.pdf") -> dict:
    """A tracking-table item for a document that finished processing.

    Written in raw DynamoDB attribute-value form on purpose: this is the shape the
    pipeline's own Lambdas persist, so ``_get_document`` is exercised against real
    stored data rather than against a Document the test constructed.
    """
    return {
        "PK": {"S": f"doc#{object_key}"},
        "SK": {"S": "none"},
        "ObjectKey": {"S": object_key},
        "ObjectStatus": {"S": "COMPLETED"},
        "PageCount": {"N": "2"},
        "QueuedTime": {"S": "2026-01-01T00:00:00Z"},
        "WorkflowStartTime": {"S": "2026-01-01T00:00:01Z"},
        "CompletionTime": {"S": "2026-01-01T00:05:00Z"},
        "WorkflowExecutionArn": {
            "S": "arn:aws:states:us-east-1:123456789012:execution:idp:previous"
        },
        "Pages": {
            "L": [
                {
                    "M": {
                        "Id": {"N": "1"},
                        "Class": {"S": "invoice"},
                        "TextUri": {"S": f"s3://{OUTPUT_BUCKET}/{object_key}/pages/1"},
                        "ClassConfidence": {"N": "0.93"},
                    }
                },
                {
                    "M": {
                        "Id": {"N": "2"},
                        "Class": {"S": "bank_statement"},
                        "TextUri": {"S": f"s3://{OUTPUT_BUCKET}/{object_key}/pages/2"},
                        "ClassConfidence": {"N": "0.81"},
                    }
                },
            ]
        },
        "Sections": {
            "L": [
                {
                    "M": {
                        "Id": {"S": "1"},
                        "Class": {"S": "invoice"},
                        "PageIds": {"L": [{"N": "1"}]},
                        "OutputJSONUri": {
                            "S": f"s3://{OUTPUT_BUCKET}/{object_key}/sections/1/result.json"
                        },
                        "ConfidenceThresholdAlerts": {
                            "L": [
                                {
                                    "M": {
                                        "attributeName": {"S": "amount"},
                                        "confidence": {"N": "0.4"},
                                        "confidenceThreshold": {"N": "0.8"},
                                    }
                                }
                            ]
                        },
                    }
                },
                {
                    "M": {
                        "Id": {"S": "2"},
                        "Class": {"S": "bank_statement"},
                        "PageIds": {"L": [{"N": "2"}]},
                        "OutputJSONUri": {
                            "S": f"s3://{OUTPUT_BUCKET}/{object_key}/sections/2/result.json"
                        },
                    }
                },
            ]
        },
    }


@pytest.fixture
def aws(aws_credentials):
    """A moto AWS with the stack deployed: real queue, real table, real buckets."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name=aws_credentials)
        cfn.create_stack(StackName=STACK_NAME, TemplateBody=_template())
        s3 = boto3.client("s3", region_name=aws_credentials)
        s3.create_bucket(Bucket=INPUT_BUCKET)
        s3.create_bucket(Bucket=OUTPUT_BUCKET)
        yield {
            "cfn": cfn,
            "s3": s3,
            "ddb": boto3.client("dynamodb", region_name=aws_credentials),
            "sqs": boto3.client("sqs", region_name=aws_credentials),
            "region": aws_credentials,
        }


@pytest.fixture
def idp_common_importable(monkeypatch):
    """Point ``_get_idp_common_path`` at the checkout's real ``idp_common_pkg``.

    The production helper cannot find it (see ``TestIdpCommonPathResolution``), and
    with it raising, every code path that reads or writes a document is
    short-circuited before it starts. The substitute is derived from the already
    imported ``idp_common`` so it always names *this* checkout, and ``sys.path`` is
    snapshotted because the code under test inserts into it and never removes the
    entry.
    """
    package_root = str(pathlib.Path(idp_common.__file__).resolve().parents[1])
    assert (pathlib.Path(package_root) / "idp_common").is_dir()
    monkeypatch.setattr(rerun_processor, "_get_idp_common_path", lambda: package_root)
    original_sys_path = list(sys.path)
    yield package_root
    sys.path[:] = original_sys_path


@pytest.fixture
def processor(aws):
    return RerunProcessor(STACK_NAME, region=aws["region"])


@pytest.fixture
def stored_document(aws):
    """One completed document in the tracking table, with its S3 artifacts."""
    aws["ddb"].put_item(TableName=TRACKING_TABLE, Item=_tracking_item())
    for section_id in ("1", "2"):
        aws["s3"].put_object(
            Bucket=OUTPUT_BUCKET,
            Key=f"lending_package.pdf/sections/{section_id}/result.json",
            Body=json.dumps({"inference_result": {"amount": "10.00"}}).encode(),
        )
    return "lending_package.pdf"


def _queue_url(aws) -> str:
    return aws["sqs"].get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]


def _queued_messages(aws) -> list:
    response = aws["sqs"].receive_message(
        QueueUrl=_queue_url(aws), MaxNumberOfMessages=10
    )
    return [json.loads(message["Body"]) for message in response.get("Messages", [])]


def _output_keys(aws) -> list:
    return sorted(
        obj["Key"]
        for obj in aws["s3"].list_objects_v2(Bucket=OUTPUT_BUCKET).get("Contents", [])
    )


def _tracking_row(aws, object_key: str = "lending_package.pdf") -> dict:
    return aws["ddb"].get_item(
        TableName=TRACKING_TABLE,
        Key={"PK": {"S": f"doc#{object_key}"}, "SK": {"S": "none"}},
    )["Item"]


def _document_with_two_sections() -> Document:
    """A processed document, built directly, for the rewind helpers."""
    return Document(
        id="lending_package.pdf",
        input_key="lending_package.pdf",
        input_bucket=INPUT_BUCKET,
        output_bucket=OUTPUT_BUCKET,
        status=Status.COMPLETED,
        num_pages=2,
        start_time="2026-01-01T00:00:01Z",
        completion_time="2026-01-01T00:05:00Z",
        workflow_execution_arn="arn:aws:states:us-east-1:1:execution:idp:previous",
        errors=["a previous run's error"],
        pages={
            "1": Page(page_id="1", classification="invoice"),
            "2": Page(page_id="2", classification="bank_statement"),
        },
        sections=[
            Section(
                section_id="1",
                classification="invoice",
                confidence=0.9,
                page_ids=["1"],
                extraction_result_uri=(
                    f"s3://{OUTPUT_BUCKET}/lending_package.pdf/sections/1/result.json"
                ),
                attributes={"amount": "10.00"},
                confidence_threshold_alerts=[{"attribute_name": "amount"}],
            ),
            Section(
                section_id="2",
                classification="bank_statement",
                confidence=0.8,
                page_ids=["2"],
                extraction_result_uri=(
                    f"s3://{OUTPUT_BUCKET}/lending_package.pdf/sections/2/result.json"
                ),
                attributes={"balance": "99.00"},
                confidence_threshold_alerts=[],
            ),
        ],
    )


@pytest.mark.unit
class TestIdpCommonPathResolution:
    """Where the processor looks for ``idp_common``, and why it never finds it."""

    def test_the_helper_cannot_resolve_idp_common_in_this_layout(self):
        """DEFECT: ``rerun_processor.py:31-42`` computes a path that never exists.

        The helper walks two directories up from its own location and then looks
        for ``lib/idp_common_pkg``. Its comment says the starting point is
        ``idp_cli/idp_cli/``, which it no longer is: this module lives at
        ``lib/idp_sdk/idp_sdk/_core/``, so two levels up is ``lib/idp_sdk`` and the
        path it builds is ``lib/idp_sdk/lib/idp_common_pkg``. That directory does
        not exist in the repository and would not exist for an installed copy
        either, where the same arithmetic lands inside ``site-packages``.

        The premise is asserted rather than assumed: if the layout ever changes so
        that the path resolves, this test fails and says so rather than quietly
        pinning nothing.
        """
        expected = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(rerun_processor.__file__))),
            "lib",
            "idp_common_pkg",
        )
        assert not os.path.exists(expected), (
            "the layout changed; _get_idp_common_path may now resolve and this "
            "test and its companion below should be revisited"
        )

        with pytest.raises(RuntimeError, match="idp_common_pkg not found"):
            _get_idp_common_path()

    def test_the_substitute_path_used_by_the_other_tests_is_this_checkout(
        self, idp_common_importable
    ):
        """Guard on the test scaffolding, not on production code.

        Every test below that touches a document relies on the substituted path.
        If that path pointed at another checkout the whole file would be green
        about the wrong code, which is the failure mode ``conftest.py``'s
        provenance guard exists to prevent — so it is checked here too.
        """
        assert idp_common_importable == str(
            pathlib.Path(idp_common.__file__).resolve().parents[1]
        )
        assert "idp_common_pkg" in idp_common_importable


@pytest.mark.unit
class TestConstruction:
    """Resource discovery, which happens entirely in ``__init__``."""

    def test_the_tracking_table_and_appsync_url_are_added_to_the_resources(
        self, processor
    ):
        """Neither is a stack *output*, so both are discovered the hard way.

        ``TrackingTable`` comes from ``DescribeStackResource`` by logical id, and
        the GraphQL URL is scraped out of the ``WebUITestEnvFile`` output — a
        multi-line ``.env`` blob. A failure here leaves the processor unable to
        read any document, which it reports as "Document not found".
        """
        assert processor.resources["TrackingTable"] == TRACKING_TABLE
        assert processor.resources["AppSyncApiUrl"] == APPSYNC_URL
        assert processor.resources["InputBucket"] == INPUT_BUCKET
        assert processor.resources["OutputBucket"] == OUTPUT_BUCKET
        assert processor.resources["DocumentQueueUrl"].endswith(QUEUE_NAME)

    def test_a_stack_without_a_tracking_table_still_constructs(self, aws):
        """Discovery failures are warnings, not errors, by design.

        A rerun processor that refused to exist would make the CLI unusable
        against a partially deployed stack; instead the key is simply absent and
        the failure surfaces later, per document. Pinned because the swallowing is
        also what makes the path defect above so quiet.
        """
        aws["cfn"].create_stack(
            StackName="idp-rerun-no-table", TemplateBody=_template(with_table=False)
        )

        processor = RerunProcessor("idp-rerun-no-table", region=aws["region"])

        assert "TrackingTable" not in processor.resources
        assert processor.resources["AppSyncApiUrl"] == APPSYNC_URL

    def test_an_env_file_output_without_the_graphql_line_leaves_the_url_unset(
        self, aws
    ):
        outputs = dict(DEFAULT_OUTPUTS)
        outputs["WebUITestEnvFile"] = "REACT_APP_AWS_REGION=us-east-1\n"
        aws["cfn"].create_stack(
            StackName="idp-rerun-no-gql",
            TemplateBody=_template(with_table=False, outputs=outputs),
        )

        processor = RerunProcessor("idp-rerun-no-gql", region=aws["region"])

        assert "AppSyncApiUrl" not in processor.resources

    def test_a_stack_with_no_outputs_at_all_leaves_the_url_unset(self, aws):
        aws["cfn"].create_stack(
            StackName="idp-rerun-no-outputs",
            TemplateBody=_template(with_table=False, outputs={}),
        )

        processor = RerunProcessor("idp-rerun-no-outputs", region=aws["region"])

        assert "AppSyncApiUrl" not in processor.resources

    def test_a_stack_that_does_not_exist_is_refused(self, aws):
        with pytest.raises(ValueError, match="not in a valid state"):
            RerunProcessor("idp-rerun-absent", region=aws["region"])

    def test_a_stack_in_the_wrong_region_reads_as_absent(self, aws):
        """Region is not a hint, it is where the stack is looked for.

        Passing the wrong region produces the same "not in a valid state" refusal
        as a missing stack, because ``DescribeStacks`` is region-scoped. Worth
        pinning as the observable behaviour of an explicit ``--region`` typo, which
        otherwise reads as "the stack is broken".
        """
        with pytest.raises(ValueError, match="not in a valid state"):
            RerunProcessor(STACK_NAME, region="us-west-2")

    def test_no_region_anywhere_is_refused_with_actionable_advice(
        self, aws, monkeypatch
    ):
        """The error must name what the operator can do about it.

        Without this check the failure would be a ``NoRegionError`` from deep
        inside botocore, raised from whichever client happened to be built first.
        """
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")

        with pytest.raises(
            ValueError, match="--region or configure AWS_DEFAULT_REGION"
        ):
            RerunProcessor(STACK_NAME)

    def test_an_ambient_region_is_picked_up_when_none_is_passed(self, aws):
        """The CLI may be invoked without ``--region``; the session supplies it.

        ``AWS_DEFAULT_REGION`` is what the ``aws_credentials`` fixture pins, so
        this is the ordinary developer-machine path, and it must reach the same
        resources an explicit region would.
        """
        processor = RerunProcessor(STACK_NAME)

        assert processor.region == aws["region"]
        assert processor.resources["TrackingTable"] == TRACKING_TABLE


@pytest.mark.unit
class TestGetDocument:
    """Reading a document back out of the real tracking table."""

    def test_a_stored_document_round_trips_with_its_pages_and_sections(
        self, processor, aws, stored_document, idp_common_importable
    ):
        document = processor._get_document(stored_document)

        assert document is not None
        assert document.status is Status.COMPLETED
        assert sorted(document.pages) == ["1", "2"]
        assert document.pages["1"].classification == "invoice"
        assert [section.section_id for section in document.sections] == ["1", "2"]
        assert document.sections[0].extraction_result_uri == (
            f"s3://{OUTPUT_BUCKET}/lending_package.pdf/sections/1/result.json"
        )

    def test_the_bucket_names_are_overwritten_from_the_stack_resources(
        self, processor, aws, stored_document, idp_common_importable
    ):
        """The tracking row does not record which buckets the document came from.

        So the processor stamps the current stack's buckets onto the document
        before re-queueing it; without this the state machine would receive a
        document with no input bucket and fail on the first S3 read.
        """
        document = processor._get_document(stored_document)

        assert document.input_bucket == INPUT_BUCKET
        assert document.output_bucket == OUTPUT_BUCKET

    def test_a_document_that_is_not_in_the_table_reads_as_none(
        self, processor, aws, idp_common_importable
    ):
        assert processor._get_document("never-processed.pdf") is None

    def test_the_environment_variables_it_borrows_are_restored(
        self, processor, aws, stored_document, idp_common_importable, monkeypatch
    ):
        """``_get_document`` mutates process-global environment to talk to DynamoDB.

        ``DocumentDynamoDBService`` is configured through ``TRACKING_TABLE`` and
        ``AWS_REGION`` rather than arguments, so the processor sets both and puts
        them back. A leak would silently re-point every *later* DynamoDB client in
        the process — including another stack's — at this stack's table.
        """
        monkeypatch.setenv("TRACKING_TABLE", "some-other-stacks-table")
        monkeypatch.setenv("AWS_REGION", aws["region"])

        processor._get_document(stored_document)

        assert os.environ["TRACKING_TABLE"] == "some-other-stacks-table"
        assert os.environ["AWS_REGION"] == aws["region"]

    def test_variables_that_were_unset_are_removed_again_not_left_behind(
        self, processor, aws, stored_document, idp_common_importable, monkeypatch
    ):
        """Restoring "absent" means deleting, not writing an empty string.

        A left-behind ``AWS_REGION`` is the one that matters: any boto3 client
        built later in the process with no explicit region would silently adopt
        this stack's region instead of the caller's configured default.
        """
        monkeypatch.delenv("TRACKING_TABLE", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)

        document = processor._get_document(stored_document)

        assert document is not None
        assert "TRACKING_TABLE" not in os.environ
        assert "AWS_REGION" not in os.environ

    def test_a_missing_tracking_table_resource_is_indistinguishable_from_no_document(
        self, processor, aws, stored_document, idp_common_importable
    ):
        """DEFECT (contributing): every failure to read becomes "not found".

        ``_get_document`` wraps its whole body in ``except Exception`` and returns
        ``None``, so a misconfigured stack, an ``AccessDenied`` on the table and a
        document that genuinely was never processed all produce the same answer,
        and ``rerun_documents`` reports all three as ``Document not found``. This
        is the mechanism that hides the path defect above, and it is why that
        defect presents as a plausible-looking per-document result rather than as
        an error.
        """
        del processor.resources["TrackingTable"]

        assert processor._get_document(stored_document) is None


@pytest.mark.unit
class TestPrepareForClassificationRerun:
    """Rewinding to classification: the destructive one."""

    def test_page_classifications_are_cleared_but_the_pages_remain(
        self, processor, aws
    ):
        """OCR output must survive; re-running OCR is the expensive part.

        The pages and their text URIs stay, only the class is blanked, so a
        classification rerun costs one Bedrock classification pass and no Textract.
        """
        document = processor._prepare_for_classification_rerun(
            _document_with_two_sections()
        )

        assert sorted(document.pages) == ["1", "2"]
        assert [page.classification for page in document.pages.values()] == ["", ""]

    def test_the_section_list_is_replaced_by_one_placeholder_not_emptied(
        self, processor, aws
    ):
        """An empty list would not propagate to the UI, so a stand-in is written.

        Note the placeholder's ``section_id`` is ``"1"``. The surrounding
        docstring and the log line both say the id is empty, so do not trust
        either: the value a consumer actually receives is ``"1"``, and a UI filter
        written against ``section_id == ""`` would not match it.
        """
        document = processor._prepare_for_classification_rerun(
            _document_with_two_sections()
        )

        assert len(document.sections) == 1
        placeholder = document.sections[0]
        assert placeholder.section_id == "1"
        assert placeholder.classification == "-"
        assert placeholder.page_ids == []
        assert placeholder.extraction_result_uri is None
        assert placeholder.attributes is None

    def test_the_document_is_rewound_to_queued_with_no_run_history(
        self, processor, aws
    ):
        document = processor._prepare_for_classification_rerun(
            _document_with_two_sections()
        )

        assert document.status is Status.QUEUED
        assert document.start_time is None
        assert document.completion_time is None
        assert document.workflow_execution_arn is None
        assert document.errors == []

    def test_the_section_extraction_results_are_deleted_from_s3(
        self, processor, aws, stored_document
    ):
        """Stale results must not survive a reclassification.

        After reclassification the section numbering may be completely different,
        so yesterday's ``sections/2/result.json`` would otherwise be picked up as
        this run's output for a section that now holds different pages — a silent
        wrong answer rather than a failure.
        """
        assert _output_keys(aws) == [
            "lending_package.pdf/sections/1/result.json",
            "lending_package.pdf/sections/2/result.json",
        ]

        processor._prepare_for_classification_rerun(_document_with_two_sections())

        assert _output_keys(aws) == []

    def test_a_section_with_no_result_uri_is_skipped(self, processor, aws):
        document = _document_with_two_sections()
        document.sections[0].extraction_result_uri = None
        document.sections[1].extraction_result_uri = "file:///tmp/not-an-s3-uri.json"

        # Neither is an s3:// URI, so no delete is attempted and none is needed.
        assert processor._prepare_for_classification_rerun(document) is document

    def test_a_delete_that_fails_does_not_abort_the_rewind(self, processor, aws):
        """A missing or unreadable artifact must not block the rerun.

        The S3 delete is best-effort cleanup; if it raised, a document whose output
        bucket had already been emptied could never be re-run at all.
        """
        document = _document_with_two_sections()
        document.sections[
            0
        ].extraction_result_uri = "s3://a-bucket-that-does-not-exist/x/result.json"

        rewound = processor._prepare_for_classification_rerun(document)

        assert rewound.status is Status.QUEUED
        assert len(rewound.sections) == 1


@pytest.mark.unit
class TestPrepareForExtractionRerun:
    """Rewinding to extraction: the conservative one."""

    def test_sections_and_classifications_survive_but_results_are_dropped(
        self, processor, aws
    ):
        """This is the whole difference from a classification rerun.

        Section boundaries and page classes are what an extraction rerun is
        deliberately *keeping*, so that only the Bedrock extraction call is paid
        for again. If these were cleared the rerun would silently become a
        full reprocess at several times the cost.
        """
        document = processor._prepare_for_extraction_rerun(
            _document_with_two_sections()
        )

        assert [section.section_id for section in document.sections] == ["1", "2"]
        assert [section.classification for section in document.sections] == [
            "invoice",
            "bank_statement",
        ]
        assert [section.page_ids for section in document.sections] == [["1"], ["2"]]
        assert [page.classification for page in document.pages.values()] == [
            "invoice",
            "bank_statement",
        ]
        for section in document.sections:
            assert section.extraction_result_uri is None
            assert section.attributes is None
            assert section.confidence_threshold_alerts == []

    def test_the_document_is_rewound_to_queued(self, processor, aws):
        document = processor._prepare_for_extraction_rerun(
            _document_with_two_sections()
        )

        assert document.status is Status.QUEUED
        assert document.start_time is None
        assert document.completion_time is None
        assert document.workflow_execution_arn is None
        assert document.errors == []

    def test_the_s3_extraction_results_are_left_in_place(
        self, processor, aws, stored_document
    ):
        """Unlike a classification rerun, nothing is deleted.

        The section numbering is unchanged, so each result object will be
        overwritten in place by the rerun. Deleting them here would turn a failed
        rerun into data loss.
        """
        processor._prepare_for_extraction_rerun(_document_with_two_sections())

        assert _output_keys(aws) == [
            "lending_package.pdf/sections/1/result.json",
            "lending_package.pdf/sections/2/result.json",
        ]


@pytest.mark.unit
class TestSendToQueue:
    """Putting the rewound document on the stack's document queue."""

    def test_the_whole_document_is_serialised_onto_the_queue(self, processor, aws):
        document = processor._prepare_for_extraction_rerun(
            _document_with_two_sections()
        )

        processor._send_to_queue(document)

        (message,) = _queued_messages(aws)
        assert message["input_key"] == "lending_package.pdf"
        assert message["status"] == "QUEUED"
        assert message["workflow_execution_arn"] is None
        assert sorted(message["pages"]) == ["1", "2"]
        assert [section["section_id"] for section in message["sections"]] == ["1", "2"]
        assert message["sections"][0]["extraction_result_uri"] is None

    def test_a_missing_queue_url_names_what_was_discovered_instead(
        self, processor, aws
    ):
        """The error lists the resources that *were* found, on purpose.

        The output is named ``DocumentQueueUrl`` while the CloudFormation resource
        is ``DocumentQueue``, and that mismatch has been the cause before, so the
        message carries the available keys to make the next occurrence diagnosable
        from one line of output.
        """
        del processor.resources["DocumentQueueUrl"]
        document = _document_with_two_sections()

        with pytest.raises(ValueError, match="DocumentQueueUrl not found"):
            processor._send_to_queue(document)

        assert _queued_messages(aws) == []


@pytest.mark.unit
class TestUpdateDocumentStatus:
    """Writing the rewound document back so the UI sees QUEUED immediately."""

    def test_the_tracking_row_is_updated_through_the_appsync_branch(
        self, processor, aws, stored_document, idp_common_importable
    ):
        """With a GraphQL URL present the processor takes the "AppSync" branch.

        That branch is now vestigial: AppSync was removed from the solution and
        ``idp_common.docs_service.create_document_service`` ignores its ``mode``
        argument, always returning the DynamoDB-backed service. So both branches
        write to the same place — asserted here against the real table rather than
        assumed, because the two branches also log different things and an operator
        reading "Updated via AppSync" would otherwise be misled about where the
        write went.
        """
        assert processor.resources["AppSyncApiUrl"] == APPSYNC_URL
        document = processor._prepare_for_extraction_rerun(
            processor._get_document(stored_document)
        )

        processor._update_document_status(document)

        row = _tracking_row(aws)
        assert row["ObjectStatus"]["S"] == "QUEUED"
        assert row["WorkflowStatus"]["S"] == "RUNNING"

    def test_the_tracking_row_is_updated_without_a_graphql_url_too(
        self, processor, aws, stored_document, idp_common_importable
    ):
        del processor.resources["AppSyncApiUrl"]
        document = processor._prepare_for_extraction_rerun(
            processor._get_document(stored_document)
        )

        processor._update_document_status(document)

        assert _tracking_row(aws)["ObjectStatus"]["S"] == "QUEUED"

    def test_the_stale_execution_arn_and_completion_time_are_not_cleared(
        self, processor, aws, stored_document, idp_common_importable
    ):
        """DEFECT: the tracking row keeps pointing at the finished previous run.

        ``_prepare_for_extraction_rerun`` sets ``workflow_execution_arn`` and
        ``completion_time`` to ``None``, but the update expression built by
        ``idp_common``'s ``_document_to_update_expressions``
        (``lib/idp_common_pkg/idp_common/dynamodb/service.py:410-419``) only emits a
        ``SET`` clause for a *truthy* value and never a ``REMOVE``. So the row ends
        up ``QUEUED`` while still carrying the previous execution's ARN and a
        completion timestamp in the past.

        Consequence: anything joining a document to its Step Functions execution —
        the UI's execution link, and any tooling that reads ``CompletionTime`` to
        decide whether a document is finished — is pointed at the wrong run until
        the rerun writes a new ARN. The SQS message is correct, so the pipeline
        itself is unaffected; this is a reporting defect. Pinned, not fixed: the
        fix is a ``REMOVE`` clause in production code.
        """
        before = _tracking_row(aws)
        document = processor._prepare_for_extraction_rerun(
            processor._get_document(stored_document)
        )

        processor._update_document_status(document)

        after = _tracking_row(aws)
        assert after["ObjectStatus"]["S"] == "QUEUED"
        assert after["WorkflowExecutionArn"] == before["WorkflowExecutionArn"]
        assert after["CompletionTime"] == before["CompletionTime"]

    def test_a_failure_to_update_is_swallowed_so_the_rerun_still_happens(
        self, processor, aws, stored_document, idp_common_importable
    ):
        """The status write is a courtesy to the UI, not part of the contract.

        The document is about to be queued regardless, and the pipeline will set
        its own status. Raising here would refuse to re-run a document over a
        cosmetic failure, so the exception is logged and dropped.
        """
        document = processor._prepare_for_extraction_rerun(
            processor._get_document(stored_document)
        )
        del processor.resources["TrackingTable"]

        processor._update_document_status(document)  # must not raise

        assert _tracking_row(aws)["ObjectStatus"]["S"] == "COMPLETED"

    def test_the_variables_it_borrows_are_put_back_as_they_were(
        self, processor, aws, stored_document, idp_common_importable, monkeypatch
    ):
        """The write path borrows three variables and must return all three.

        ``APPSYNC_API_URL`` is the one with a visible consequence outside this
        process: a leaked value would point some later ``idp_common`` caller at
        another deployment's API.
        """
        document = processor._prepare_for_extraction_rerun(
            processor._get_document(stored_document)
        )
        monkeypatch.setenv("APPSYNC_API_URL", "https://someone-elses.example/graphql")
        monkeypatch.setenv("TRACKING_TABLE", "some-other-stacks-table")

        processor._update_document_status(document)

        assert os.environ["APPSYNC_API_URL"] == "https://someone-elses.example/graphql"
        assert os.environ["TRACKING_TABLE"] == "some-other-stacks-table"
        assert os.environ["AWS_REGION"] == aws["region"]

    def test_every_variable_that_was_unset_is_removed_again(
        self, processor, aws, stored_document, idp_common_importable, monkeypatch
    ):
        """All three borrowed variables are cleaned up, not just the table name.

        The document is fetched before the variables are cleared, so the write path
        is the only thing running with them absent — which is what an SDK caller's
        process looks like when it never set them at all.
        """
        document = processor._prepare_for_extraction_rerun(
            processor._get_document(stored_document)
        )
        monkeypatch.delenv("APPSYNC_API_URL", raising=False)
        monkeypatch.delenv("TRACKING_TABLE", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)

        processor._update_document_status(document)

        assert _tracking_row(aws)["ObjectStatus"]["S"] == "QUEUED"
        assert "APPSYNC_API_URL" not in os.environ
        assert "TRACKING_TABLE" not in os.environ
        assert "AWS_REGION" not in os.environ


@pytest.mark.unit
class TestRerunDocumentsAsShipped:
    """``rerun_documents`` with the production path helper in place."""

    def test_every_document_is_reported_not_found_even_when_it_exists(
        self, processor, aws, stored_document
    ):
        """DEFECT: the rerun feature is inoperable, and reports a plausible lie.

        With ``_get_idp_common_path`` raising (see
        ``TestIdpCommonPathResolution``), ``_get_document`` catches the
        ``RuntimeError`` and returns ``None``, which ``rerun_documents`` reports as
        ``Document not found``. So a rerun of a document that is sitting in the
        tracking table, with its artifacts in S3, queues nothing, deletes nothing,
        updates nothing, and returns a result an operator would read as "that
        document isn't there" — sending them to look at the wrong thing.

        Every document fails this way, so the defect is total rather than
        intermittent. This test deliberately does *not* use the
        ``idp_common_importable`` fixture: it is the as-shipped behaviour.
        """
        result = processor.rerun_documents([stored_document], step="classification")

        assert result == {
            "documents_queued": 0,
            "documents_failed": 1,
            "failed_documents": [
                {"object_key": stored_document, "error": "Document not found"}
            ],
            "step": "classification",
        }
        assert _queued_messages(aws) == []
        assert _output_keys(aws) == [
            "lending_package.pdf/sections/1/result.json",
            "lending_package.pdf/sections/2/result.json",
        ]
        assert _tracking_row(aws)["ObjectStatus"]["S"] == "COMPLETED"


@pytest.mark.unit
class TestRerunDocumentsEndToEnd:
    """``rerun_documents`` with ``idp_common`` reachable, so the real path runs."""

    def test_a_classification_rerun_queues_resets_and_deletes(
        self, processor, aws, stored_document, idp_common_importable
    ):
        """One document through the whole sequence, checked in all three places.

        The three writes have to agree: the queue message is what the pipeline
        acts on, the tracking row is what the UI shows, and the deleted S3 objects
        are what stops a stale result being read as this run's output. A change
        that updated only two of the three would look correct in any single
        assertion.
        """
        result = processor.rerun_documents([stored_document], step="classification")

        assert result["documents_queued"] == 1
        assert result["documents_failed"] == 0

        (message,) = _queued_messages(aws)
        assert message["status"] == "QUEUED"
        assert message["input_bucket"] == INPUT_BUCKET
        assert [section["section_id"] for section in message["sections"]] == ["1"]
        assert message["sections"][0]["classification"] == "-"
        assert all(page["classification"] == "" for page in message["pages"].values())

        row = _tracking_row(aws)
        assert row["ObjectStatus"]["S"] == "QUEUED"
        assert [section["M"]["Class"]["S"] for section in row["Sections"]["L"]] == ["-"]

        assert _output_keys(aws) == []

    def test_an_extraction_rerun_keeps_the_sections_and_the_artifacts(
        self, processor, aws, stored_document, idp_common_importable
    ):
        result = processor.rerun_documents([stored_document], step="extraction")

        assert result["documents_queued"] == 1

        (message,) = _queued_messages(aws)
        assert [section["section_id"] for section in message["sections"]] == ["1", "2"]
        assert message["sections"][0]["classification"] == "invoice"
        assert message["sections"][0]["extraction_result_uri"] is None

        assert _output_keys(aws) == [
            "lending_package.pdf/sections/1/result.json",
            "lending_package.pdf/sections/2/result.json",
        ]

    def test_one_missing_document_does_not_stop_the_others(
        self, processor, aws, stored_document, idp_common_importable
    ):
        """A partial failure must be partial.

        Reruns are issued in bulk from a batch id, so a single document that was
        cleaned out of the tracking table cannot be allowed to abandon the rest.
        """
        result = processor.rerun_documents(
            ["never-processed.pdf", stored_document], step="extraction"
        )

        assert result["documents_queued"] == 1
        assert result["documents_failed"] == 1
        assert result["failed_documents"] == [
            {"object_key": "never-processed.pdf", "error": "Document not found"}
        ]
        assert len(_queued_messages(aws)) == 1

    def test_an_unrecognised_step_is_reported_per_document_not_raised(
        self, processor, aws, stored_document, idp_common_importable
    ):
        """A mistyped step name becomes a per-document failure, not an error.

        The ``ValueError`` is raised inside the per-document ``try``, so the call
        returns normally with every document marked failed and nothing queued.
        The consequence worth knowing: a caller that only checks for an exception
        sees success, and one that only checks ``documents_queued`` against zero
        cannot tell a typo from an empty batch. The error string in
        ``failed_documents`` is the only place the typo is visible.
        """
        result = processor.rerun_documents([stored_document], step="summarization")

        assert result["documents_queued"] == 0
        assert result["failed_documents"] == [
            {"object_key": stored_document, "error": "Invalid step: summarization"}
        ]
        assert _queued_messages(aws) == []
        assert _tracking_row(aws)["ObjectStatus"]["S"] == "COMPLETED"

    def test_an_empty_document_list_is_a_no_op(self, processor, aws):
        result = processor.rerun_documents([], step="extraction")

        assert result == {
            "documents_queued": 0,
            "documents_failed": 0,
            "failed_documents": [],
            "step": "extraction",
        }


@pytest.mark.unit
class TestGetBatchDocumentIds:
    """Expanding a batch id into the document keys a rerun should cover."""

    def _store_batch(self, aws, batch_id: str, metadata: dict) -> None:
        aws["s3"].put_object(
            Bucket=OUTPUT_BUCKET,
            Key=f"cli-batches/{batch_id}/metadata.json",
            Body=json.dumps(metadata).encode(),
        )

    def test_the_document_ids_come_back_from_the_stored_batch_metadata(
        self, processor, aws
    ):
        self._store_batch(
            aws,
            "batch-2026-01-01",
            {"document_ids": ["a.pdf", "b.pdf"], "batch_id": "batch-2026-01-01"},
        )

        assert processor.get_batch_document_ids("batch-2026-01-01") == [
            "a.pdf",
            "b.pdf",
        ]

    def test_an_unknown_batch_is_refused_rather_than_treated_as_empty(
        self, processor, aws
    ):
        """An empty list here would mean "rerun nothing" and report success.

        A mistyped batch id has to be an error, because the alternative is a rerun
        that silently does nothing and a caller that believes it ran.
        """
        with pytest.raises(ValueError, match="Batch not found: batch-absent"):
            processor.get_batch_document_ids("batch-absent")

    def test_batch_metadata_with_no_document_ids_yields_an_empty_list(
        self, processor, aws
    ):
        self._store_batch(aws, "batch-empty", {"batch_id": "batch-empty"})

        assert processor.get_batch_document_ids("batch-empty") == []
