# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_sdk.operations.batch`` — the ``client.batch`` namespace.

``BatchOperation`` is the public surface of batch processing. It does four things
of its own and delegates the rest: it decides which source a caller meant and
refuses an ambiguous one, it collapses the ``config_profile`` / ``config_version``
spelling pair, it translates raw dictionaries from ``_core`` into the SDK's
Pydantic models, and it converts failures into the SDK's exception types
(``IDPConfigurationError`` for a bad request, ``IDPResourceNotFoundError`` for a
batch or resource that is not there, ``IDPProcessingError`` for everything else).
Two operations are not delegation at all and carry real logic:
``_process_test_set``, which drives two Lambda functions and builds the document
list from the test-set bucket, and ``_get_document_metadata``, which mirrors an
``explainability_info`` tree into a confidence tree.

What shaped these tests:

**The exception type is the contract, and where the ``try`` starts decides it.**
Argument validation happens *before* the ``try`` in ``process``, ``reprocess`` and
``delete_documents``, so an ``IDPConfigurationError`` propagates as itself; anything
raised inside becomes an ``IDPProcessingError``. The same applies to
``resolve_config_profile``, which raises a plain ``ValueError`` from before the
``try``. Each of those is asserted by type, since a caller's ``except`` clause is
written against it.

**Delegation is tested through the real collaborator wherever one can exist.**
Every test here runs inside ``moto.mock_aws`` against a real CloudFormation stack,
real S3 buckets and a real DynamoDB table, so ``BatchProcessor`` is constructed the
way production constructs it and the operations that only forward arguments are
checked by what actually lands in S3. ``ProgressMonitor``, ``AssessmentAnalyzer``,
``RerunProcessor`` and ``WorkflowStopper`` are mocked: all four reach AWS services
moto cannot usefully fake here (Lambda invoke needs Docker, Step Functions needs a
running state machine), and for those the assertions are on the arguments they were
handed and on the model built from what they returned.

**Configuration pinning is followed all the way to the object.** ``process``
decides per source method, by inspecting its signature, whether to pass
``config_version`` and ``config_revision`` at all, so what a caller pinned can be
dropped without any error. Those outcomes are asserted from the uploaded object's
user metadata rather than from a call record.

The last class covers ``idp_sdk.models.batch.BatchListResult``'s sequence
protocol, which is what lets an older caller treat a paginated result as a list.
"""

import base64
import json
import os
import warnings
from unittest.mock import Mock, patch

import boto3
import pytest
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk.exceptions import (
    IDPConfigurationError,
    IDPProcessingError,
    IDPResourceNotFoundError,
)
from idp_sdk.models import (
    BatchDownloadResult,
    BatchInfo,
    BatchListResult,
    BatchProcessResult,
    RerunStep,
)

STACK_NAME = "test-idp-stack"
BARE_STACK = "test-idp-stack-bare"
INPUT_BUCKET = "idp-input-bucket"
OUTPUT_BUCKET = "idp-output-bucket"
TESTSET_BUCKET = "idp-testset-bucket"
SOURCE_BUCKET = "someone-elses-bucket"
TRACKING_TABLE = "idp-tracking-table"

_OUTPUTS = {
    "S3InputBucketName": INPUT_BUCKET,
    "S3OutputBucketName": OUTPUT_BUCKET,
    "S3TestSetBucketName": TESTSET_BUCKET,
    "LambdaLookupFunctionName": "idp-lookup-function",
}


def _template(outputs, physical_suffix=""):
    """A stack of the shape ``StackInfo`` requires: a ``DocumentQueue``, a
    ``TrackingTable`` and the outputs the resource map is built from."""
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
    """A live moto account holding two stacks and their buckets.

    ``BARE_STACK`` publishes no bucket outputs, which is how a stack deployed
    without the resources an operation needs is represented: ``StackInfo`` maps a
    missing output to the empty string.
    """
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name=aws_credentials)
        cfn.create_stack(StackName=STACK_NAME, TemplateBody=_template(_OUTPUTS))
        cfn.create_stack(
            StackName=BARE_STACK,
            TemplateBody=_template(
                {"LambdaLookupFunctionName": "idp-lookup-function"},
                physical_suffix="-bare",
            ),
        )
        s3_client = boto3.client("s3", region_name=aws_credentials)
        for bucket in (INPUT_BUCKET, OUTPUT_BUCKET, TESTSET_BUCKET, SOURCE_BUCKET):
            s3_client.create_bucket(Bucket=bucket)
        yield aws_credentials


@pytest.fixture
def client(idp_stack):
    return IDPClient(stack_name=STACK_NAME, region=idp_stack)


@pytest.fixture
def s3(idp_stack):
    return boto3.client("s3", region_name=idp_stack)


def _write_pdf(directory, name, body=b"%PDF-1.4 fake"):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return str(path)


def _write_manifest(tmp_path, paths, name="manifest.csv"):
    lines = ["document_path,baseline_source"] + [f"{path}," for path in paths]
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _store_batch(s3_client, batch_id, document_ids, **extra):
    """Write the batch metadata record ``get_batch_info`` reads."""
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
    )
    return record


def _keys(s3_client, bucket, prefix=""):
    response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return sorted(obj["Key"] for obj in response.get("Contents", []))


def _local_tree(root):
    found = []
    for directory, _subdirs, files in os.walk(root):
        for name in files:
            found.append(os.path.relpath(os.path.join(directory, name), root))
    return sorted(found)


def _metadata(s3_client, key):
    return s3_client.head_object(Bucket=INPUT_BUCKET, Key=key)["Metadata"]


@pytest.mark.unit
@pytest.mark.batch
class TestProcessSourceResolution:
    """Which of the four mutually exclusive sources a call meant."""

    def test_a_directory_source_is_detected_and_scanned(self, client, s3, tmp_path):
        _write_pdf(tmp_path, "a.pdf")

        result = client.batch.process(source=str(tmp_path), batch_id="b")

        assert isinstance(result, BatchProcessResult)
        assert _keys(s3, INPUT_BUCKET) == ["b/a.pdf"]

    def test_a_file_source_is_treated_as_a_manifest(self, client, s3, tmp_path):
        local = _write_pdf(tmp_path / "docs", "a.pdf")
        manifest = _write_manifest(tmp_path, [local])

        result = client.batch.process(source=manifest, batch_id="b")

        assert result.source == manifest
        assert _keys(s3, INPUT_BUCKET) == ["b/a.pdf"]

    def test_an_s3_source_is_treated_as_a_uri(self, client, s3):
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/a.pdf", Body=b"x")

        result = client.batch.process(source=f"s3://{SOURCE_BUCKET}/in/", batch_id="b")

        assert result.source == f"s3://{SOURCE_BUCKET}/in/"
        assert _keys(s3, INPUT_BUCKET) == ["b/a.pdf"]

    def test_a_source_that_is_neither_a_path_nor_a_uri_is_a_configuration_error(
        self, client
    ):
        """Raised before the ``try``, so it must not arrive as an
        ``IDPProcessingError`` — a caller distinguishes "you asked wrongly" from
        "the run failed"."""
        with pytest.raises(IDPConfigurationError, match="not found or unrecognized"):
            client.batch.process(source="/no/such/path.csv")

    def test_no_source_at_all_is_a_configuration_error(self, client):
        with pytest.raises(IDPConfigurationError, match="Specify exactly one source"):
            client.batch.process()

    def test_two_sources_are_a_configuration_error(self, client, tmp_path):
        """Ambiguity is refused rather than resolved by precedence: a caller who
        passed both would otherwise silently process only one of them."""
        _write_pdf(tmp_path, "a.pdf")

        with pytest.raises(IDPConfigurationError, match="Specify exactly one source"):
            client.batch.process(
                directory=str(tmp_path), s3_uri=f"s3://{SOURCE_BUCKET}/in/"
            )

    def test_a_missing_stack_name_is_a_configuration_error(self, idp_stack, tmp_path):
        _write_pdf(tmp_path, "a.pdf")
        stackless = IDPClient(region=idp_stack)

        with pytest.raises(IDPConfigurationError, match="stack_name is required"):
            stackless.batch.process(directory=str(tmp_path))

    def test_a_per_call_stack_name_overrides_the_client_default(
        self, idp_stack, tmp_path
    ):
        """The override is resolved before the processor is built, so naming a
        stack that does not exist fails on that stack, not on the default."""
        _write_pdf(tmp_path, "a.pdf")
        client = IDPClient(stack_name=STACK_NAME, region=idp_stack)

        with pytest.raises(IDPProcessingError, match="no-such-stack"):
            client.batch.process(directory=str(tmp_path), stack_name="no-such-stack")


@pytest.mark.unit
@pytest.mark.batch
class TestProcessConfigPinning:
    """``config_profile``/``config_version`` and the revision that goes with it."""

    def test_the_two_spellings_are_one_argument(self, client, s3, tmp_path):
        _write_pdf(tmp_path, "a.pdf")

        client.batch.process(
            directory=str(tmp_path), batch_id="b", config_profile="tuned"
        )

        assert _metadata(s3, "b/a.pdf") == {"config-version": "tuned"}

    def test_conflicting_spellings_are_refused_rather_than_reconciled(
        self, client, tmp_path
    ):
        """Choosing one silently would run the caller's batch under a
        configuration they did not name, so this is a ``ValueError`` from before
        the ``try`` and reaches the caller unwrapped."""
        _write_pdf(tmp_path, "a.pdf")

        with pytest.raises(ValueError, match="two names for the same argument"):
            client.batch.process(
                directory=str(tmp_path),
                config_profile="tuned",
                config_version="other",
            )

    def test_the_same_value_under_both_spellings_is_accepted(
        self, client, s3, tmp_path
    ):
        _write_pdf(tmp_path, "a.pdf")

        client.batch.process(
            directory=str(tmp_path),
            batch_id="b",
            config_profile="tuned",
            config_version="tuned",
        )

        assert _metadata(s3, "b/a.pdf") == {"config-version": "tuned"}

    def test_a_manifest_batch_carries_the_profile_and_the_revision_through(
        self, client, s3, tmp_path
    ):
        """Both values have to survive the signature inspection in ``process``
        and reach the uploaded object's metadata, which is the only channel the
        queue processor reads them from."""
        local = _write_pdf(tmp_path / "docs", "a.pdf")
        manifest = _write_manifest(tmp_path, [local])

        client.batch.process(
            manifest=manifest,
            batch_id="b",
            config_profile="tuned",
            config_revision=4,
        )

        assert _metadata(s3, "b/a.pdf") == {
            "config-version": "tuned",
            "config-revision": "4",
        }

    def test_a_directory_batch_forwards_the_revision_and_still_loses_it(
        self, client, s3, tmp_path
    ):
        """DEFECT, pinned as-is: ``batch_processor.py:242`` seen from the public API.

        ``process`` does its part — ``process_batch_from_directory`` accepts
        ``config_revision``, so the signature check passes and the value is
        forwarded. The processor then calls ``_process_documents`` positionally and
        never passes it on, so the uploaded object carries the profile without the
        revision, and the batch runs under whatever revision the profile currently
        holds. Compare the manifest case directly above, which keeps both.

        The failure is silent by construction: nothing raises, the counts are
        right, and only the object's user metadata shows it. When the processor is
        fixed, this test fails and the expectation becomes the two-key dict.
        """
        _write_pdf(tmp_path, "a.pdf")

        client.batch.process(
            directory=str(tmp_path),
            batch_id="b",
            config_profile="tuned",
            config_revision=4,
        )

        assert _metadata(s3, "b/a.pdf") == {"config-version": "tuned"}

    def test_an_s3_uri_batch_silently_ignores_the_pinned_profile(self, client, s3):
        """Pinned as current behaviour, and it is a trap.

        ``process`` passes ``config_version`` only if the target method's
        signature accepts it, and ``process_batch_from_s3_uri`` takes neither
        ``config_version`` nor ``config_revision``. So a caller who pins a profile
        for an S3-sourced batch gets no error and no metadata: the batch runs
        under whatever the default configuration is. If that method gains the
        parameters, this test fails and the expectation becomes the pinned values.
        """
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/a.pdf", Body=b"x")

        client.batch.process(
            s3_uri=f"s3://{SOURCE_BUCKET}/in/",
            batch_id="b",
            config_profile="tuned",
            config_revision=4,
        )

        assert _metadata(s3, "b/a.pdf") == {}


@pytest.mark.unit
@pytest.mark.batch
class TestProcessResultMapping:
    """The raw processor dict becomes a ``BatchProcessResult``."""

    def test_the_counts_are_renamed_and_the_timestamp_is_parsed(
        self, client, s3, tmp_path
    ):
        _write_pdf(tmp_path, "a.pdf")
        _write_pdf(tmp_path, "b.pdf")

        result = client.batch.process(directory=str(tmp_path), batch_id="b")

        assert result.documents_queued == 2
        assert result.documents_uploaded == 2
        assert result.documents_failed == 0
        assert result.baselines_uploaded == 0
        assert result.output_prefix == "sdk-batch"
        assert result.timestamp.utcoffset().total_seconds() == 0
        assert sorted(result.document_ids) == ["b/a.pdf", "b/b.pdf"]

    @patch("idp_sdk._core.batch_processor.BatchProcessor")
    def test_absent_optional_keys_fall_back_to_defaults(self, processor_cls, client):
        """A processor returning only the two required keys must still produce a
        valid model — the counts default to zero and the prefix falls back to the
        one the caller asked for, rather than raising a validation error."""
        processor = Mock()
        processor.process_batch.return_value = {
            "batch_id": "b",
            "document_ids": ["b/a.pdf"],
        }
        processor_cls.return_value = processor

        result = client.batch.process(manifest="whatever.csv", batch_prefix="mine")

        assert (result.documents_queued, result.documents_failed) == (0, 0)
        assert result.source == ""
        assert result.output_prefix == "mine"

    @patch("idp_sdk._core.batch_processor.BatchProcessor")
    def test_a_processor_failure_becomes_a_processing_error_that_keeps_its_cause(
        self, processor_cls, client
    ):
        processor = Mock()
        processor.process_batch.side_effect = RuntimeError("bucket exploded")
        processor_cls.return_value = processor

        with pytest.raises(IDPProcessingError, match="bucket exploded") as raised:
            client.batch.process(manifest="whatever.csv")

        assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.unit
@pytest.mark.batch
class TestRunIsDeprecated:
    """``run()`` is the former name of ``process()``."""

    def test_it_warns_and_still_processes(self, client, s3, tmp_path):
        _write_pdf(tmp_path, "a.pdf")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = client.batch.run(directory=str(tmp_path), batch_id="b")

        assert any(item.category is DeprecationWarning for item in caught)
        assert result.documents_queued == 1
        assert _keys(s3, INPUT_BUCKET) == ["b/a.pdf"]

    def test_the_profile_alias_is_collapsed_before_delegating(
        self, client, s3, tmp_path
    ):
        """``run`` resolves the alias itself and then passes the result on as
        ``config_version``; if it forwarded ``config_profile`` too, ``process``
        would see the same value under both names."""
        _write_pdf(tmp_path, "a.pdf")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            client.batch.run(
                directory=str(tmp_path), batch_id="b", config_profile="tuned"
            )

        assert _metadata(s3, "b/a.pdf") == {"config-version": "tuned"}


class _FakeLambda:
    """A stand-in for the Lambda client ``_process_test_set`` builds.

    moto cannot invoke a Lambda without Docker, so the two invocations are
    recorded and answered here. ``function_names`` decides what ``list_functions``
    reports, which is how the operation locates the resolver and the runner.
    """

    def __init__(self, function_names, runner_result=None, resolver_error=None):
        self.function_names = function_names
        self.runner_result = runner_result or {"testRunId": "run-1", "filesCount": 2}
        self.resolver_error = resolver_error
        self.invocations = []

    def get_paginator(self, operation_name):
        assert operation_name == "list_functions"
        pages = [
            {"Functions": [{"FunctionName": name} for name in self.function_names]}
        ]
        return Mock(paginate=Mock(return_value=pages))

    def invoke(self, FunctionName, Payload):  # noqa: N803 - boto3 parameter name
        self.invocations.append((FunctionName, json.loads(Payload)))
        if "TestSetResolverFunction" in FunctionName:
            if self.resolver_error:
                raise self.resolver_error
            return {"Payload": Mock(read=Mock(return_value=b"{}"))}
        body = json.dumps(self.runner_result).encode("utf-8")
        return {"Payload": Mock(read=Mock(return_value=body))}


@pytest.fixture
def fake_lambda(monkeypatch, idp_stack):
    """Route ``boto3.client("lambda")`` to a fake and leave every other service on
    moto, so the S3 listing in ``_process_test_set`` is a real one."""
    real_client = boto3.client
    holder = {}

    def dispatch(service_name, *args, **kwargs):
        if service_name == "lambda":
            return holder["fake"]
        return real_client(service_name, *args, **kwargs)

    monkeypatch.setattr(boto3, "client", dispatch)

    def install(function_names, **kwargs):
        holder["fake"] = _FakeLambda(function_names, **kwargs)
        return holder["fake"]

    return install


@pytest.mark.unit
@pytest.mark.batch
class TestProcessTestSet:
    """Running a stored test set through the TestRunner Lambda."""

    RESOLVER = f"{STACK_NAME}-TestSetResolverFunction-abc"
    RUNNER = f"{STACK_NAME}-TestRunnerFunction-abc"

    def test_the_runner_payload_carries_everything_the_caller_pinned(
        self, client, fake_lambda
    ):
        """A run that ignored ``configRevision`` would score a different
        configuration than the caller believes, so each field is asserted."""
        lambda_client = fake_lambda([self.RESOLVER, self.RUNNER])

        client.batch._process_test_set(
            processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
            test_set="set-a",
            context="nightly",
            number_of_files=5,
            config_version="tuned",
            config_revision=4,
        )

        runner_call = next(
            payload
            for name, payload in lambda_client.invocations
            if "TestRunnerFunction" in name
        )
        assert runner_call["arguments"]["input"] == {
            "testSetId": "set-a",
            "context": "nightly",
            "numberOfFiles": 5,
            "configVersion": "tuned",
            "configRevision": 4,
        }

    def test_a_revision_given_as_a_string_is_coerced_to_an_integer(
        self, client, fake_lambda
    ):
        """The payload is JSON, and a quoted number is not the same value to the
        runner as a number."""
        lambda_client = fake_lambda([self.RESOLVER, self.RUNNER])

        client.batch._process_test_set(
            processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
            test_set="set-a",
            context=None,
            number_of_files=None,
            config_revision="4",
        )

        runner_call = next(
            payload
            for name, payload in lambda_client.invocations
            if "TestRunnerFunction" in name
        )
        assert runner_call["arguments"]["input"]["configRevision"] == 4

    def test_optional_fields_are_left_out_rather_than_sent_as_null(
        self, client, fake_lambda
    ):
        lambda_client = fake_lambda([self.RESOLVER, self.RUNNER])

        client.batch._process_test_set(
            processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
            test_set="set-a",
            context=None,
            number_of_files=None,
        )

        runner_call = next(
            payload
            for name, payload in lambda_client.invocations
            if "TestRunnerFunction" in name
        )
        assert runner_call["arguments"]["input"] == {"testSetId": "set-a"}

    def test_the_resolver_is_invoked_first_when_it_exists(self, client, fake_lambda):
        lambda_client = fake_lambda([self.RESOLVER, self.RUNNER])

        client.batch._process_test_set(
            processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
            test_set="set-a",
            context=None,
            number_of_files=None,
        )

        assert [name for name, _ in lambda_client.invocations] == [
            self.RESOLVER,
            self.RUNNER,
        ]

    def test_a_missing_resolver_is_not_fatal(self, client, fake_lambda, caplog):
        """Auto-detection is a convenience; an older stack without the resolver
        must still be able to run a test set."""
        lambda_client = fake_lambda([self.RUNNER])

        with caplog.at_level("WARNING"):
            result = client.batch._process_test_set(
                processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
                test_set="set-a",
                context=None,
                number_of_files=None,
            )

        assert [name for name, _ in lambda_client.invocations] == [self.RUNNER]
        assert result["batch_id"] == "run-1"
        assert "TestSetResolverFunction not found" in caplog.text

    def test_a_failing_resolver_is_not_fatal(self, client, fake_lambda, caplog):
        lambda_client = fake_lambda(
            [self.RESOLVER, self.RUNNER], resolver_error=RuntimeError("throttled")
        )

        with caplog.at_level("WARNING"):
            result = client.batch._process_test_set(
                processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
                test_set="set-a",
                context=None,
                number_of_files=None,
            )

        assert result["batch_id"] == "run-1"
        assert "non-fatal" in caplog.text
        assert len(lambda_client.invocations) == 2

    def test_a_missing_runner_is_a_resource_not_found_error(self, client, fake_lambda):
        fake_lambda([self.RESOLVER])

        with pytest.raises(IDPResourceNotFoundError, match="TestRunnerFunction"):
            client.batch._process_test_set(
                processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
                test_set="set-a",
                context=None,
                number_of_files=None,
            )

    def test_a_function_belonging_to_another_stack_is_not_used(
        self, client, fake_lambda
    ):
        """Function discovery matches on the stack name being a substring of the
        function name, so a second deployment's runner must not be picked up."""
        fake_lambda(["other-stack-TestRunnerFunction-xyz"])

        with pytest.raises(IDPResourceNotFoundError, match="TestRunnerFunction"):
            client.batch._process_test_set(
                processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
                test_set="set-a",
                context=None,
                number_of_files=None,
            )

    def test_a_function_error_in_the_payload_is_a_processing_error(
        self, client, fake_lambda
    ):
        """Lambda answers ``StatusCode=200`` for an invocation that ran and threw,
        so the only signal is ``errorMessage`` in the payload. Missing it would
        report a failed test run as a successful one with no documents."""
        fake_lambda(
            [self.RESOLVER, self.RUNNER],
            runner_result={"errorMessage": "no such test set", "errorType": "KeyError"},
        )

        with pytest.raises(IDPProcessingError, match=r"\(KeyError\): no such test set"):
            client.batch._process_test_set(
                processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
                test_set="set-a",
                context=None,
                number_of_files=None,
            )

    def test_document_ids_are_built_from_the_test_set_input_prefix(
        self, client, fake_lambda, s3
    ):
        """Each document id is ``<testRunId>/<filename>`` — the key the results
        will be written under, not the key the input was read from. Directory
        markers in the listing must not become documents."""
        fake_lambda([self.RESOLVER, self.RUNNER])
        s3.put_object(Bucket=TESTSET_BUCKET, Key="set-a/input/", Body=b"")
        s3.put_object(Bucket=TESTSET_BUCKET, Key="set-a/input/a.pdf", Body=b"a")
        s3.put_object(Bucket=TESTSET_BUCKET, Key="set-a/input/b.pdf", Body=b"b")
        s3.put_object(Bucket=TESTSET_BUCKET, Key="set-b/input/other.pdf", Body=b"o")

        result = client.batch._process_test_set(
            processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
            test_set="set-a",
            context=None,
            number_of_files=None,
        )

        assert sorted(result["document_ids"]) == ["run-1/a.pdf", "run-1/b.pdf"]
        assert result["source"] == "test-set:set-a"
        assert result["output_prefix"] == "set-a"
        assert result["queued"] == 2
        assert result["uploaded"] == 0

    def test_the_reported_file_count_comes_from_the_runner_not_the_listing(
        self, client, fake_lambda, s3
    ):
        """``filesCount`` is what the runner actually queued; the S3 listing is
        only used to name the documents."""
        fake_lambda([self.RUNNER], runner_result={"testRunId": "r", "filesCount": 9})
        s3.put_object(Bucket=TESTSET_BUCKET, Key="set-a/input/a.pdf", Body=b"a")

        result = client.batch._process_test_set(
            processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
            test_set="set-a",
            context=None,
            number_of_files=None,
        )

        assert result["queued"] == 9
        assert result["document_ids"] == ["r/a.pdf"]

    def test_an_empty_input_prefix_yields_no_documents(self, client, fake_lambda):
        fake_lambda([self.RUNNER], runner_result={"testRunId": "r"})

        result = client.batch._process_test_set(
            processor=Mock(resources={"TestSetBucket": TESTSET_BUCKET}),
            test_set="set-a",
            context=None,
            number_of_files=None,
        )

        assert result["document_ids"] == []
        assert result["queued"] == 0

    def test_process_routes_a_test_set_and_returns_the_public_model(
        self, client, fake_lambda, s3
    ):
        fake_lambda([self.RESOLVER, self.RUNNER])
        s3.put_object(Bucket=TESTSET_BUCKET, Key="set-a/input/a.pdf", Body=b"a")

        result = client.batch.process(test_set="set-a")

        assert isinstance(result, BatchProcessResult)
        assert result.batch_id == "run-1"
        assert result.source == "test-set:set-a"


@pytest.mark.unit
@pytest.mark.batch
class TestReprocess:
    """Re-running documents from a chosen pipeline step."""

    def test_neither_documents_nor_a_batch_is_a_configuration_error(self, client):
        with pytest.raises(
            IDPConfigurationError, match="either document_ids or batch_id"
        ):
            client.batch.reprocess(step="extraction")

    @patch("idp_sdk._core.rerun_processor.RerunProcessor")
    def test_explicit_document_ids_are_forwarded_with_monitoring_off(
        self, processor_cls, client
    ):
        """The SDK call is synchronous and returns counts; monitoring belongs to
        the caller, so ``monitor=False`` is part of the contract."""
        processor = Mock()
        processor.rerun_documents.return_value = {
            "documents_queued": 2,
            "documents_failed": 1,
            "failed_documents": [{"document_id": "c.pdf", "error": "boom"}],
        }
        processor_cls.return_value = processor

        result = client.batch.reprocess(
            step=RerunStep.EXTRACTION, document_ids=["a.pdf", "b.pdf", "c.pdf"]
        )

        processor.rerun_documents.assert_called_once_with(
            document_ids=["a.pdf", "b.pdf", "c.pdf"], step="extraction", monitor=False
        )
        processor.get_batch_document_ids.assert_not_called()
        assert result.documents_queued == 2
        assert result.documents_failed == 1
        assert result.failed_documents == [{"document_id": "c.pdf", "error": "boom"}]
        assert result.step is RerunStep.EXTRACTION

    @patch("idp_sdk._core.rerun_processor.RerunProcessor")
    def test_a_batch_id_is_expanded_into_its_documents(self, processor_cls, client):
        processor = Mock()
        processor.get_batch_document_ids.return_value = ["b/a.pdf", "b/b.pdf"]
        processor.rerun_documents.return_value = {"documents_queued": 2}
        processor_cls.return_value = processor

        result = client.batch.reprocess(step="classification", batch_id="b")

        processor.get_batch_document_ids.assert_called_once_with("b")
        processor.rerun_documents.assert_called_once_with(
            document_ids=["b/a.pdf", "b/b.pdf"], step="classification", monitor=False
        )
        assert result.documents_failed == 0

    @patch("idp_sdk._core.rerun_processor.RerunProcessor")
    def test_explicit_documents_win_over_a_batch_id(self, processor_cls, client):
        processor = Mock()
        processor.rerun_documents.return_value = {"documents_queued": 1}
        processor_cls.return_value = processor

        client.batch.reprocess(
            step="extraction", document_ids=["only.pdf"], batch_id="b"
        )

        processor.get_batch_document_ids.assert_not_called()

    @patch("idp_sdk._core.rerun_processor.RerunProcessor")
    def test_a_step_that_is_not_a_pipeline_step_is_rejected(
        self, processor_cls, client
    ):
        """``RerunStep(step_str)`` is constructed inside the ``try``, so an unknown
        step surfaces as an ``IDPProcessingError`` even though it is really a bad
        argument. Pinned so the type is on record."""
        processor = Mock()
        processor.rerun_documents.return_value = {"documents_queued": 1}
        processor_cls.return_value = processor

        with pytest.raises(IDPProcessingError, match="Reprocess failed"):
            client.batch.reprocess(step="nonsense", document_ids=["a.pdf"])

    @patch("idp_sdk._core.rerun_processor.RerunProcessor")
    def test_a_processor_failure_becomes_a_processing_error(
        self, processor_cls, client
    ):
        processor_cls.side_effect = RuntimeError("no state machine")

        with pytest.raises(IDPProcessingError, match="no state machine"):
            client.batch.reprocess(step="extraction", document_ids=["a.pdf"])

    @patch("idp_sdk._core.rerun_processor.RerunProcessor")
    def test_rerun_is_the_deprecated_spelling_of_reprocess(self, processor_cls, client):
        processor = Mock()
        processor.rerun_documents.return_value = {"documents_queued": 1}
        processor_cls.return_value = processor

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = client.batch.rerun(step="extraction", document_ids=["a.pdf"])

        assert any(item.category is DeprecationWarning for item in caught)
        assert result.documents_queued == 1


@pytest.mark.unit
@pytest.mark.batch
class TestGetDocumentIds:
    """Reading a batch's document list without starting anything."""

    def test_the_stored_document_ids_are_returned(self, client, s3):
        _store_batch(s3, "b", ["b/a.pdf", "b/c.pdf"])

        assert client.batch.get_document_ids("b") == ["b/a.pdf", "b/c.pdf"]

    def test_an_unknown_batch_is_a_resource_not_found_error(self, client):
        """Re-raised deliberately rather than wrapped: the caller's ``except
        IDPResourceNotFoundError`` is how a confirmation prompt distinguishes a
        typo in the batch id from an infrastructure failure."""
        with pytest.raises(IDPResourceNotFoundError, match="Batch not found: b"):
            client.batch.get_document_ids("b")

    def test_a_record_with_no_document_ids_reads_as_an_empty_batch(self, client, s3):
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key="cli-batches/b/metadata.json",
            Body=json.dumps({"batch_id": "b"}),
        )

        assert client.batch.get_document_ids("b") == []

    def test_an_unusable_stack_becomes_a_processing_error(self, client):
        with pytest.raises(IDPProcessingError, match="Failed to get document IDs"):
            client.batch.get_document_ids("b", stack_name="no-such-stack")


@pytest.mark.unit
@pytest.mark.batch
class TestGetStatus:
    """Per-document status for a batch, summarised."""

    STATUS_DATA = {
        "completed": [
            {
                "document_id": "b/a.pdf",
                "status": "COMPLETED",
                "start_time": "2026-01-01T00:00:00",
                "end_time": "2026-01-01T00:01:00",
                "duration": 60,
                "num_pages": 3,
                "num_sections": 2,
            }
        ],
        "running": [{"document_id": "b/b.pdf", "status": "RUNNING"}],
        "queued": [{"document_id": "b/c.pdf", "status": "QUEUED"}],
        "failed": [
            {"document_id": "b/d.pdf", "status": "FAILED", "error": "timed out"}
        ],
        "all_complete": False,
        "total": 4,
    }

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_every_category_is_reported_and_the_rate_is_a_fraction(
        self, monitor_cls, client, s3
    ):
        """``ProgressMonitor`` reports the success rate as a percentage and the
        model documents it as 0-1, so the division is the part that breaks
        silently: 50.0 instead of 0.5 still validates."""
        _store_batch(s3, "b", ["b/a.pdf", "b/b.pdf", "b/c.pdf", "b/d.pdf"])
        monitor = Mock()
        monitor.get_batch_status.return_value = self.STATUS_DATA
        monitor.calculate_statistics.return_value = {
            "total": 4,
            "completed": 1,
            "failed": 1,
            "running": 1,
            "queued": 1,
            "success_rate": 50.0,
            "all_complete": False,
        }
        monitor_cls.return_value = monitor

        status = client.batch.get_status("b")

        assert status.total == 4
        assert (status.completed, status.failed) == (1, 1)
        assert status.in_progress == 1
        assert status.queued == 1
        assert status.success_rate == 0.5
        assert status.all_complete is False
        assert [doc.document_id for doc in status.documents] == [
            "b/a.pdf",
            "b/b.pdf",
            "b/c.pdf",
            "b/d.pdf",
        ]
        completed = status.documents[0]
        assert (completed.duration_seconds, completed.num_pages) == (60, 3)
        assert status.documents[3].error == "timed out"

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_the_monitor_is_asked_about_exactly_the_batch_documents(
        self, monitor_cls, client, s3
    ):
        _store_batch(s3, "b", ["b/a.pdf"])
        monitor = Mock()
        monitor.get_batch_status.return_value = {
            "completed": [],
            "running": [],
            "queued": [],
            "failed": [],
            "all_complete": True,
            "total": 1,
        }
        monitor.calculate_statistics.return_value = {"total": 1}
        monitor_cls.return_value = monitor

        client.batch.get_status("b")

        monitor.get_batch_status.assert_called_once_with(["b/a.pdf"])

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_empty_timestamps_become_none_rather_than_empty_strings(
        self, monitor_cls, client, s3
    ):
        """``DocumentStatus`` types these as datetimes, so an empty string from
        the tracking table would fail validation for a document that has simply
        not started yet."""
        _store_batch(s3, "b", ["b/a.pdf"])
        monitor = Mock()
        monitor.get_batch_status.return_value = {
            "completed": [],
            "running": [],
            "queued": [
                {"document_id": "b/a.pdf", "status": "QUEUED", "start_time": ""}
            ],
            "failed": [],
            "all_complete": False,
            "total": 1,
        }
        monitor.calculate_statistics.return_value = {"total": 1}
        monitor_cls.return_value = monitor

        status = client.batch.get_status("b")

        assert status.documents[0].start_time is None
        assert status.documents[0].end_time is None

    def test_an_unknown_batch_is_a_resource_not_found_error(self, client):
        with pytest.raises(IDPResourceNotFoundError, match="Batch not found"):
            client.batch.get_status("b")


@pytest.mark.unit
@pytest.mark.batch
class TestList:
    """Listing recent batches."""

    def test_records_become_batch_info_models_newest_first(self, client, s3):
        _store_batch(s3, "run-20260101-000000", ["run-20260101-000000/a.pdf"])
        _store_batch(s3, "run-20260301-000000", ["run-20260301-000000/a.pdf"])

        listing = client.batch.list()

        assert isinstance(listing, BatchListResult)
        assert [item.batch_id for item in listing.batches] == [
            "run-20260301-000000",
            "run-20260101-000000",
        ]
        assert listing.count == 2
        assert listing.next_token is None

    def test_a_truncated_listing_carries_a_cursor_that_fetches_the_rest(
        self, client, s3
    ):
        """DEFECT, pinned as-is: ``batch_processor.py:876-882``, surfaced here.

        The newest-first sort is applied to one page, *after* S3 has already
        chosen which prefixes that page holds — and S3 lists ascending. So the
        first page of a limited listing holds the **oldest** batches, ordered
        newest-first among themselves, and the newest batch is on the last page.

        The observable consequence is a listing that looks right and is wrong:
        ``client.batch.list()`` defaults to ``limit=10``, so on a stack with more
        than ten batches it returns the ten oldest while presenting them in
        descending order — indistinguishable, at a glance, from the ten most
        recent. Here, with two batches and ``limit=1``, page one is ``batch-1``
        and the newer ``batch-2`` is only reachable through the cursor.

        Fixing it means listing all prefixes before sorting and slicing; this test
        then fails, and the expectation becomes ``batch-2`` first.
        """
        for name in ("batch-1", "batch-2"):
            _store_batch(s3, name, [f"{name}/a.pdf"])

        first = client.batch.list(limit=1)
        assert [item.batch_id for item in first.batches] == ["batch-1"]
        assert first.next_token is not None

        second = client.batch.list(limit=1, next_token=first.next_token)

        assert [item.batch_id for item in second.batches] == ["batch-2"]
        assert second.next_token is None

    def test_a_missing_timestamp_reads_as_empty_rather_than_failing_validation(
        self, client, s3
    ):
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key="cli-batches/b/metadata.json",
            Body=json.dumps({"batch_id": "b", "document_ids": []}),
        )

        listing = client.batch.list()

        assert listing.batches[0].timestamp == ""
        assert listing.batches[0].queued == 0


@pytest.mark.unit
@pytest.mark.batch
class TestDownloadResults:
    """Pulling a batch's outputs down through the public API."""

    @staticmethod
    def _put_outputs(s3_client):
        for key in (
            "b/a.pdf/pages/1/text.json",
            "b/a.pdf/sections/1/result.json",
            "b/a.pdf/summary/summary.json",
            "b/a.pdf/evaluation/report.json",
            "b/a.pdf/attachments/original.zip",
        ):
            s3_client.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=key.encode())

    def test_the_default_expands_to_the_four_known_types_and_no_others(
        self, client, s3, tmp_path
    ):
        """``all`` is expanded here into an explicit list, so an output directory
        the SDK does not know about is *not* downloaded — unlike the processor's
        own ``all``, which takes everything. Pinned because the two layers use the
        same word for different sets."""
        self._put_outputs(s3)
        destination = tmp_path / "out"

        result = client.batch.download_results("b", str(destination))

        assert isinstance(result, BatchDownloadResult)
        assert result.files_downloaded == 4
        assert result.documents_downloaded == 1
        assert result.output_dir == str(destination)
        assert not (destination / "b" / "a.pdf" / "attachments").exists()

    def test_an_explicit_type_list_is_honoured(self, client, s3, tmp_path):
        self._put_outputs(s3)
        destination = tmp_path / "out"

        result = client.batch.download_results(
            "b", str(destination), file_types=["sections"]
        )

        assert result.files_downloaded == 1
        assert _local_tree(destination) == [
            os.path.join("b", "a.pdf", "sections", "1", "result.json")
        ]

    def test_all_named_explicitly_is_expanded_the_same_way(self, client, s3, tmp_path):
        self._put_outputs(s3)
        destination = tmp_path / "out"

        result = client.batch.download_results(
            "b", str(destination), file_types=["all"]
        )

        assert result.files_downloaded == 4


@pytest.mark.unit
@pytest.mark.batch
class TestVersions:
    """A document's processing-run history and one run's exact output."""

    def test_list_versions_propagates_the_missing_resource_key(self, client):
        """DEFECT, pinned as-is: consequence at the public API of
        ``batch_processor.py:1019``.

        ``BatchProcessor.list_document_versions`` reads
        ``resources["TrackingTable"]``, a key ``StackInfo`` never produces (it
        records the table as ``DocumentsTable``). ``list_versions`` adds no
        ``try``, so the caller receives a bare ``KeyError`` naming an internal
        resource key instead of a version list or an SDK exception. The operation
        cannot succeed against any stack.
        """
        with pytest.raises(KeyError, match="TrackingTable"):
            client.batch.list_versions("b/a.pdf")

    def test_download_version_reports_one_document(self, client, s3, tmp_path):
        """``documents_downloaded`` is hardcoded to 1 here because the operation is
        addressed by a single document id, whatever the manifest holds."""
        key = "b/a.pdf/sections/1/result.json"
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=b"pinned bytes")
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key="b/a.pdf/runs/run-1/manifest.json",
            Body=json.dumps({"files": [{"key": key}]}),
        )
        destination = tmp_path / "out"

        result = client.batch.download_version("b/a.pdf", "run-1", str(destination))

        assert isinstance(result, BatchDownloadResult)
        assert result.files_downloaded == 1
        assert result.documents_downloaded == 1
        assert result.output_dir == os.path.join(str(destination), "run-1")
        landed = destination / "run-1" / key
        assert landed.read_bytes() == b"pinned bytes"


@pytest.mark.unit
@pytest.mark.batch
class TestDownloadSources:
    """Fetching the original inputs back out of the input bucket."""

    def test_each_document_lands_at_its_key_below_the_output_directory(
        self, client, s3, tmp_path
    ):
        """The document id is an S3 key and may contain slashes, so the local
        tree mirrors it — a flattened download would collide two documents with
        the same basename."""
        s3.put_object(Bucket=INPUT_BUCKET, Key="b/one/a.pdf", Body=b"first")
        s3.put_object(Bucket=INPUT_BUCKET, Key="b/two/a.pdf", Body=b"second")
        _store_batch(s3, "b", ["b/one/a.pdf", "b/two/a.pdf"])
        destination = tmp_path / "sources"

        result = client.batch.download_sources("b", str(destination))

        assert result.files_downloaded == 2
        assert result.documents_downloaded == 2
        assert _local_tree(destination) == [
            os.path.join("b", "one", "a.pdf"),
            os.path.join("b", "two", "a.pdf"),
        ]
        assert (destination / "b" / "two" / "a.pdf").read_bytes() == b"second"

    def test_an_unknown_batch_is_a_resource_not_found_error(self, client, tmp_path):
        """The batch is checked before the output directory is created, so nothing
        is written for a batch that does not exist."""
        destination = tmp_path / "never-written"

        with pytest.raises(IDPResourceNotFoundError, match="Batch not found"):
            client.batch.download_sources("b", str(destination))

        assert not destination.exists()

    def test_a_document_whose_source_is_gone_becomes_a_processing_error(
        self, client, s3, tmp_path
    ):
        """The input object may have been deleted after the batch ran, and the
        partial download is not rolled back — the error names the failure rather
        than reporting a short count as success."""
        _store_batch(s3, "b", ["b/deleted.pdf"])

        with pytest.raises(IDPProcessingError, match="Failed to download sources"):
            client.batch.download_sources("b", str(tmp_path / "sources"))


@pytest.mark.unit
@pytest.mark.batch
class TestDeleteDocuments:
    """Permanent deletion of documents and their derived data."""

    def test_neither_a_batch_nor_a_pattern_is_refused(self, client):
        with pytest.raises(IDPConfigurationError, match="either batch_id or pattern"):
            client.batch.delete_documents()

    def test_both_a_batch_and_a_pattern_are_refused(self, client):
        """The two select different document sets, so honouring one silently
        would delete something the caller did not ask to delete."""
        with pytest.raises(IDPConfigurationError, match="Cannot specify both"):
            client.batch.delete_documents(batch_id="b", pattern="b/*.pdf")

    def test_a_stack_without_the_required_resources_is_a_resource_error(self, client):
        with pytest.raises(IDPResourceNotFoundError, match="Required resources"):
            client.batch.delete_documents(batch_id="b", stack_name=BARE_STACK)

    def test_selecting_nothing_deletes_nothing_and_says_so(self, client):
        """A pattern matching no document must not reach the deleter at all, and
        must still report success — an empty selection is not an error."""
        with patch("idp_common.delete_documents.get_documents_by_pattern") as select:
            with patch("idp_common.delete_documents.delete_documents") as delete:
                select.return_value = []

                result = client.batch.delete_documents(pattern="b/*.pdf")

        delete.assert_not_called()
        assert result.success is True
        assert (result.deleted_count, result.failed_count, result.total_count) == (
            0,
            0,
            0,
        )
        assert result.results == []

    def test_a_pattern_selects_through_the_pattern_helper(self, client):
        with patch("idp_common.delete_documents.get_documents_by_pattern") as select:
            with patch("idp_common.delete_documents.delete_documents") as delete:
                select.return_value = ["b/a.pdf"]
                delete.return_value = {
                    "success": True,
                    "deleted_count": 1,
                    "failed_count": 0,
                    "total_count": 1,
                    "dry_run": True,
                    "results": [
                        {
                            "success": True,
                            "object_key": "b/a.pdf",
                            "deleted": {"s3": 3},
                            "errors": [],
                        }
                    ],
                }

                result = client.batch.delete_documents(
                    pattern="b/*.pdf", status_filter="FAILED", dry_run=True
                )

        kwargs = select.call_args.kwargs
        assert kwargs["pattern"] == "b/*.pdf"
        assert kwargs["status_filter"] == "FAILED"
        assert kwargs["tracking_table"].name == TRACKING_TABLE
        assert delete.call_args.kwargs["object_keys"] == ["b/a.pdf"]
        assert delete.call_args.kwargs["dry_run"] is True
        assert delete.call_args.kwargs["input_bucket"] == INPUT_BUCKET
        assert delete.call_args.kwargs["output_bucket"] == OUTPUT_BUCKET
        assert result.dry_run is True
        assert result.deleted_count == 1
        assert result.results[0].object_key == "b/a.pdf"
        assert result.results[0].deleted == {"s3": 3}

    def test_a_batch_id_selects_through_the_batch_helper(self, client):
        """The two helpers scope differently — the batch one matches a leading
        path segment, the pattern one a wildcard — so which is called decides
        which documents are destroyed."""
        with patch("idp_common.delete_documents.get_documents_by_batch") as select:
            with patch("idp_common.delete_documents.delete_documents") as delete:
                select.return_value = ["b/a.pdf"]
                delete.return_value = {
                    "success": True,
                    "deleted_count": 1,
                    "failed_count": 0,
                    "total_count": 1,
                    "results": [],
                }

                result = client.batch.delete_documents(
                    batch_id="b", continue_on_error=False
                )

        assert select.call_args.kwargs["batch_id"] == "b"
        assert delete.call_args.kwargs["continue_on_error"] is False
        assert result.success is True

    def test_a_failure_inside_deletion_becomes_a_processing_error(self, client):
        with patch("idp_common.delete_documents.get_documents_by_batch") as select:
            select.side_effect = RuntimeError("table on fire")

            with pytest.raises(IDPProcessingError, match="Batch deletion failed"):
                client.batch.delete_documents(batch_id="b")


RESULT_JSON = {
    "document_class": {"type": "Bank Statement"},
    "inference_result": {
        "account_number": "12345",
        "transactions": [{"amount": "10.00"}, {"amount": "20.00"}],
        "metadata": {"dropped": True},
        "explainability_info": {"dropped": True},
    },
    "explainability_info": [
        {
            "account_number": {"confidence": 0.97},
            "transactions": [
                {"amount": {"confidence": 0.9}},
                {"amount": {"confidence": 0.8}},
            ],
            "address": {"city": {"confidence": 0.5}},
        }
    ],
    "split_document": {"page_indices": [0, 1, 2]},
}


def _completed_monitor():
    monitor = Mock()
    monitor.get_batch_status.side_effect = lambda ids: {"completed": list(ids)}
    return monitor


@pytest.mark.unit
@pytest.mark.batch
class TestGetResults:
    """Extracted fields for a page of a batch's documents."""

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_fields_confidence_and_page_count_are_read_from_the_result_object(
        self, monitor_cls, client, s3
    ):
        """``metadata`` and ``explainability_info`` are stripped from ``fields``
        because they are bookkeeping, and the confidence tree must mirror the
        shape of the values — including per-row confidence for a list, which a
        flattening implementation would drop."""
        monitor_cls.return_value = _completed_monitor()
        _store_batch(s3, "b", ["b/a.pdf"])
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key="b/a.pdf/sections/1/result.json",
            Body=json.dumps(RESULT_JSON),
        )

        page = client.batch.get_results("b")

        assert page["batch_id"] == "b"
        assert page["section_id"] == 1
        assert page["count"] == 1
        assert page["total_in_batch"] == 1
        assert "next_token" not in page
        document = page["documents"][0]
        assert document["status"] == "COMPLETED"
        assert document["document_class"] == "Bank Statement"
        assert set(document["fields"]) == {"account_number", "transactions"}
        assert document["confidence"] == {
            "account_number": 0.97,
            # A list is mirrored as a list of same-shaped entries, so row 2's
            # score stays attached to row 2's field.
            "transactions": [{"amount": 0.9}, {"amount": 0.8}],
            "address": {"city": 0.5},
        }
        assert document["page_count"] == 3

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_an_explainability_node_with_no_score_mirrors_as_null(
        self, monitor_cls, client, s3
    ):
        """A node the assessment step did not score must appear in the confidence
        tree as ``None``, keeping the tree the same shape as the fields. Omitting
        it would make an unscored field indistinguishable from an absent one."""
        monitor_cls.return_value = _completed_monitor()
        _store_batch(s3, "b", ["b/a.pdf"])
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key="b/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "inference_result": {"address": {"city": "Seattle"}},
                    "explainability_info": [{"address": {"city": "not-a-score"}}],
                }
            ),
        )

        document = client.batch.get_results("b")["documents"][0]

        assert document["confidence"] == {"address": {"city": None}}

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_a_multi_instance_section_reports_its_instances(
        self, monitor_cls, client, s3
    ):
        """A multi-instance class puts every document it found under one
        ``instances`` list; reporting ``None`` there would hide all but the raw
        fields from a caller iterating documents."""
        monitor_cls.return_value = _completed_monitor()
        _store_batch(s3, "b", ["b/a.pdf"])
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key="b/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "document_class": {"type": "Cheque"},
                    "inference_result": {
                        "instances": [{"amount": "1.00"}, {"amount": "2.00"}]
                    },
                }
            ),
        )

        document = client.batch.get_results("b")["documents"][0]

        assert document["instances"] == [{"amount": "1.00"}, {"amount": "2.00"}]

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_a_section_with_no_result_object_still_reports_its_status(
        self, monitor_cls, client, s3
    ):
        """The S3 read is wrapped separately, so a completed document whose
        section 9 was never written reports COMPLETED with empty fields rather
        than ERROR."""
        monitor_cls.return_value = _completed_monitor()
        _store_batch(s3, "b", ["b/a.pdf"])

        document = client.batch.get_results("b", section_id=9)["documents"][0]

        assert document["status"] == "COMPLETED"
        assert document["fields"] is None
        assert document["confidence"] is None
        assert document["page_count"] is None

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_each_non_terminal_status_is_reported_without_reading_results(
        self, monitor_cls, client, s3
    ):
        _store_batch(s3, "b", ["b/a.pdf"])
        for category, expected in (
            ("running", "RUNNING"),
            ("queued", "QUEUED"),
            ("failed", "FAILED"),
            ("nothing", "UNKNOWN"),
        ):
            monitor = Mock()
            monitor.get_batch_status.return_value = {category: ["b/a.pdf"]}
            monitor_cls.return_value = monitor

            document = client.batch.get_results("b")["documents"][0]

            assert document["status"] == expected, category
            assert document["fields"] is None

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_a_lookup_failure_is_reported_as_an_error_row_not_a_raise(
        self, monitor_cls, client, s3
    ):
        """One unreachable document must not fail the whole page."""
        _store_batch(s3, "b", ["b/a.pdf"])
        monitor = Mock()
        monitor.get_batch_status.side_effect = RuntimeError("lookup lambda gone")
        monitor_cls.return_value = monitor

        document = client.batch.get_results("b")["documents"][0]

        assert document == {
            "document_id": "b/a.pdf",
            "document_class": None,
            "fields": None,
            "instances": None,
            "confidence": None,
            "page_count": None,
            "status": "ERROR",
        }

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_paging_walks_the_document_list_without_gaps_or_repeats(
        self, monitor_cls, client, s3
    ):
        """The cursor is a base64-encoded offset into the batch's document list."""
        monitor_cls.return_value = _completed_monitor()
        _store_batch(s3, "b", ["b/a.pdf", "b/b.pdf", "b/c.pdf"])

        first = client.batch.get_results("b", limit=2)
        assert [doc["document_id"] for doc in first["documents"]] == [
            "b/a.pdf",
            "b/b.pdf",
        ]
        assert base64.b64decode(first["next_token"]).decode() == "2"

        second = client.batch.get_results("b", limit=2, next_token=first["next_token"])

        assert [doc["document_id"] for doc in second["documents"]] == ["b/c.pdf"]
        assert "next_token" not in second
        assert second["total_in_batch"] == 3

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_an_unreadable_cursor_restarts_from_the_beginning(
        self, monitor_cls, client, s3
    ):
        """Pinned as current behaviour: a corrupted cursor silently re-reads the
        first page instead of reporting that it could not be decoded."""
        monitor_cls.return_value = _completed_monitor()
        _store_batch(s3, "b", ["b/a.pdf", "b/b.pdf"])
        corrupt_cursor = "%%%not-base64%%%"

        page = client.batch.get_results("b", limit=1, next_token=corrupt_cursor)

        assert [doc["document_id"] for doc in page["documents"]] == ["b/a.pdf"]

    def test_an_unknown_batch_is_a_resource_not_found_error(self, client):
        with pytest.raises(IDPResourceNotFoundError, match="Batch not found"):
            client.batch.get_results("b")


@pytest.mark.unit
@pytest.mark.batch
class TestGetConfidence:
    """Per-attribute confidence for a page of a batch's documents."""

    @patch("idp_sdk._core.assessment_analyzer.AssessmentAnalyzer")
    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_attributes_and_status_are_reported_per_document(
        self, monitor_cls, analyzer_cls, client, s3
    ):
        monitor_cls.return_value = _completed_monitor()
        analyzer = Mock()
        analyzer.get_confidence.return_value = {
            "attributes": {"account_number": {"confidence": 0.97}}
        }
        analyzer_cls.return_value = analyzer
        _store_batch(s3, "b", ["b/a.pdf"])

        page = client.batch.get_confidence("b", section_id=2)

        analyzer.get_confidence.assert_called_once_with("b/a.pdf", 2)
        assert page["section_id"] == 2
        assert page["documents"] == [
            {
                "document_id": "b/a.pdf",
                "attributes": {"account_number": {"confidence": 0.97}},
                "status": "COMPLETED",
            }
        ]

    @patch("idp_sdk._core.assessment_analyzer.AssessmentAnalyzer")
    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_a_document_with_no_assessment_yet_reads_as_processing(
        self, monitor_cls, analyzer_cls, client, s3
    ):
        """An absent assessment file is the normal state mid-run, so it is
        reported as PROCESSING rather than as an error."""
        monitor_cls.return_value = _completed_monitor()
        analyzer = Mock()
        analyzer.get_confidence.side_effect = FileNotFoundError("not written yet")
        analyzer_cls.return_value = analyzer
        _store_batch(s3, "b", ["b/a.pdf"])

        page = client.batch.get_confidence("b")

        assert page["documents"][0]["status"] == "PROCESSING"
        assert page["documents"][0]["attributes"] == {}

    @patch("idp_sdk._core.assessment_analyzer.AssessmentAnalyzer")
    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_any_other_failure_reads_as_an_error_row(
        self, monitor_cls, analyzer_cls, client, s3, caplog
    ):
        monitor_cls.return_value = _completed_monitor()
        analyzer = Mock()
        analyzer.get_confidence.side_effect = RuntimeError("malformed assessment")
        analyzer_cls.return_value = analyzer
        _store_batch(s3, "b", ["b/a.pdf"])

        with caplog.at_level("WARNING"):
            page = client.batch.get_confidence("b")

        assert page["documents"][0]["status"] == "ERROR"
        assert "Error retrieving confidence" in caplog.text

    @patch("idp_sdk._core.assessment_analyzer.AssessmentAnalyzer")
    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_each_status_category_is_mapped(
        self, monitor_cls, analyzer_cls, client, s3
    ):
        analyzer = Mock()
        analyzer.get_confidence.return_value = {}
        analyzer_cls.return_value = analyzer
        _store_batch(s3, "b", ["b/a.pdf"])

        for category, expected in (
            ("running", "RUNNING"),
            ("queued", "QUEUED"),
            ("failed", "FAILED"),
            ("nothing", "UNKNOWN"),
        ):
            monitor = Mock()
            monitor.get_batch_status.return_value = {category: ["b/a.pdf"]}
            monitor_cls.return_value = monitor

            page = client.batch.get_confidence("b")

            assert page["documents"][0]["status"] == expected, category
            # An analyzer answer with no attributes key must still be a dict.
            assert page["documents"][0]["attributes"] == {}

    @patch("idp_sdk._core.assessment_analyzer.AssessmentAnalyzer")
    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_paging_walks_the_document_list(
        self, monitor_cls, analyzer_cls, client, s3
    ):
        monitor_cls.return_value = _completed_monitor()
        analyzer = Mock()
        analyzer.get_confidence.return_value = {"attributes": {}}
        analyzer_cls.return_value = analyzer
        _store_batch(s3, "b", ["b/a.pdf", "b/b.pdf", "b/c.pdf"])

        first = client.batch.get_confidence("b", limit=2)
        second = client.batch.get_confidence(
            "b", limit=2, next_token=first["next_token"]
        )

        assert [doc["document_id"] for doc in first["documents"]] == [
            "b/a.pdf",
            "b/b.pdf",
        ]
        assert [doc["document_id"] for doc in second["documents"]] == ["b/c.pdf"]
        assert "next_token" not in second

    @patch("idp_sdk._core.assessment_analyzer.AssessmentAnalyzer")
    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_an_unreadable_cursor_restarts_from_the_beginning(
        self, monitor_cls, analyzer_cls, client, s3
    ):
        monitor_cls.return_value = _completed_monitor()
        analyzer = Mock()
        analyzer.get_confidence.return_value = {"attributes": {}}
        analyzer_cls.return_value = analyzer
        _store_batch(s3, "b", ["b/a.pdf", "b/b.pdf"])
        corrupt_cursor = "%%%not-base64%%%"

        page = client.batch.get_confidence("b", limit=1, next_token=corrupt_cursor)

        assert [doc["document_id"] for doc in page["documents"]] == ["b/a.pdf"]

    def test_an_unknown_batch_is_a_resource_not_found_error(self, client):
        with pytest.raises(IDPResourceNotFoundError, match="Batch not found"):
            client.batch.get_confidence("b")


@pytest.mark.unit
@pytest.mark.batch
class TestStopWorkflows:
    """Halting everything in flight for a stack."""

    @patch("idp_sdk._core.stop_workflows.WorkflowStopper")
    def test_both_halves_of_the_stopper_result_are_typed(self, stopper_cls, client):
        stopper = Mock()
        stopper.stop_all.return_value = {
            "executions_stopped": {
                "total_stopped": 3,
                "total_failed": 1,
                "remaining": 2,
                "error": "throttled",
            },
            "documents_aborted": {"documents_aborted": 4, "error": None},
        }
        stopper_cls.return_value = stopper

        result = client.batch.stop_workflows()

        stopper.stop_all.assert_called_once_with(skip_purge=False, skip_stop=False)
        assert result.executions_stopped.total_stopped == 3
        assert result.executions_stopped.total_failed == 1
        assert result.executions_stopped.remaining == 2
        assert result.executions_stopped.error == "throttled"
        assert result.documents_aborted.documents_aborted == 4
        assert result.queue_purged is True

    @patch("idp_sdk._core.stop_workflows.WorkflowStopper")
    def test_skipped_steps_are_reflected_and_absent_halves_are_none(
        self, stopper_cls, client
    ):
        """``queue_purged`` is derived from the request, not from the stopper's
        answer, so skipping the purge must report ``False``."""
        stopper = Mock()
        stopper.stop_all.return_value = {}
        stopper_cls.return_value = stopper

        result = client.batch.stop_workflows(skip_purge=True, skip_stop=True)

        stopper.stop_all.assert_called_once_with(skip_purge=True, skip_stop=True)
        assert result.executions_stopped is None
        assert result.documents_aborted is None
        assert result.queue_purged is False


@pytest.mark.unit
@pytest.mark.batch
class TestBatchListResultSequenceProtocol:
    """``models.batch.BatchListResult`` doubles as a list of its batches.

    The pagination fields only exist on the model, so callers written before
    pagination existed keep iterating, measuring and indexing the result itself.
    """

    @staticmethod
    def _result():
        batches = [
            BatchInfo(
                batch_id=name,
                document_ids=[f"{name}/a.pdf"],
                queued=1,
                failed=0,
                timestamp="2026-01-01T00:00:00+00:00",
            )
            for name in ("batch-1", "batch-2")
        ]
        cursor = base64.b64encode(b"2").decode()
        return BatchListResult(batches=batches, count=2, next_token=cursor)

    def test_iteration_yields_the_batches(self):
        assert [item.batch_id for item in self._result()] == ["batch-1", "batch-2"]

    def test_len_counts_the_batches_it_holds(self):
        """Note this is ``len(batches)``, not the ``count`` field, so the two can
        disagree if a caller constructs the model with an inconsistent count."""
        assert len(self._result()) == 2

    def test_indexing_selects_a_batch(self):
        assert self._result()[1].batch_id == "batch-2"

    def test_an_out_of_range_index_still_raises(self):
        with pytest.raises(IndexError):
            self._result()[5]
