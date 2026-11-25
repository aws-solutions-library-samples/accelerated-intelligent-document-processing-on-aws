# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The SDK reprocesses documents by asking the stack's reprocess resolver.

``client.document.reprocess``, ``client.batch.reprocess`` and
``client.document.reprocess_options`` hold no reprocess logic: they invoke the
resolver Lambda (its ARN is an output of the nested API resolver stack) with an
event that carries no identity, and report what it answers for each document.

What is replaced here: CloudFormation (a stand-in for ``StackInfo`` that answers
the one output lookup) and the Lambda service (a real botocore client behind a
``Stubber``, so an ``Invoke`` parameter the service does not have fails the test).
Everything between the public method and ``Invoke`` is the real code.
"""

from __future__ import annotations

import io
import warnings

import pytest
from botocore.response import StreamingBody

from idp_sdk import (
    BatchReprocessResult,
    DocumentReprocessResult,
    IDPClient,
    IDPConfigurationError,
    IDPError,
    IDPProcessingError,
    IDPResourceNotFoundError,
    IDPValidationError,
    ReprocessOption,
    ReprocessRefusedError,
    RerunStep,
)
from idp_sdk._core import reprocess as server
from idp_sdk._core import resolver_invoke

pytestmark = pytest.mark.unit

STACK = "idp-reprocess-test"
REGION = "us-east-1"


def _queued(key: str, start: str = "extraction") -> dict:
    return {
        "objectKey": key,
        "status": "QUEUED",
        "startStep": start,
        "reason": None,
        "message": None,
    }


def _refused(key: str, reason: str, message: str) -> dict:
    return {
        "objectKey": key,
        "status": "REFUSED",
        "startStep": None,
        "reason": reason,
        "message": message,
    }


@pytest.fixture(autouse=True)
def stack(resolver_stack):
    resolver_stack.has(server.REPROCESS_OUTPUT)
    return resolver_stack


@pytest.fixture
def resolver(api_resolver):
    return api_resolver


@pytest.fixture
def client():
    return IDPClient(stack_name=STACK, region=REGION)


# ------------------------------------------------------------------ one document


class TestOneDocument:
    def test_the_request_names_the_document_and_the_step_and_no_caller(
        self, client, resolver
    ):
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["batch/invoice.pdf"], "startStep": "extraction"},
            {"queued": 1, "results": [_queued("batch/invoice.pdf")]},
        )

        result = client.document.reprocess("batch/invoice.pdf", step="extraction")

        resolver.done()
        assert isinstance(result, DocumentReprocessResult)
        assert result.document_id == "batch/invoice.pdf"
        assert result.queued is True
        assert result.status == "QUEUED"
        assert result.step is RerunStep.EXTRACTION
        assert result.start_step == "extraction"
        assert (result.reason, result.message) == (None, None)
        assert resolver.regions == [REGION]

    def test_the_step_defaults_to_the_beginning(self, client, resolver):
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "beginning"},
            {"queued": 1, "results": [_queued("a.pdf", "beginning")]},
        )

        assert client.document.reprocess("a.pdf").step is RerunStep.BEGINNING
        resolver.done()

    def test_an_enum_step_is_sent_as_its_value(self, client, resolver):
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "policy_rule_validation"},
            {"queued": 1, "results": [_queued("a.pdf", "policy_rule_validation")]},
        )

        client.document.reprocess("a.pdf", step=RerunStep.POLICY_RULE_VALIDATION)

        resolver.done()

    def test_skip_version_and_revision_are_sent_when_given(self, client, resolver):
        resolver.answers(
            "reprocessDocument",
            {
                "objectKeys": ["a.pdf"],
                "version": "claims",
                "revision": 4,
                "startStep": "extraction",
                "skipSteps": ["summarization", "evaluation"],
            },
            {"queued": 1, "results": [_queued("a.pdf")]},
        )

        client.document.reprocess(
            "a.pdf",
            step="extraction",
            skip=["summarization", RerunStep.EVALUATION],
            version="claims",
            revision=4,
        )

        resolver.done()

    def test_revision_zero_is_sent(self, client, resolver):
        """Zero is a revision; only an absent one is left out."""
        resolver.answers(
            "reprocessDocument",
            {
                "objectKeys": ["a.pdf"],
                "version": "p",
                "revision": 0,
                "startStep": "ocr",
            },
            {"queued": 1, "results": [_queued("a.pdf", "beginning")]},
        )

        result = client.document.reprocess("a.pdf", step="ocr", version="p", revision=0)

        resolver.done()
        assert result.step is RerunStep.OCR
        assert result.start_step == "beginning"

    def test_resume_reports_the_step_the_server_chose(self, client, resolver):
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "resume"},
            {"queued": 1, "results": [_queued("a.pdf", "assessment")]},
        )

        result = client.document.reprocess("a.pdf", step=RerunStep.RESUME)

        assert (result.step, result.start_step) == (RerunStep.RESUME, "assessment")

    def test_a_refused_document_is_a_result_and_not_an_error(self, client, resolver):
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "assessment"},
            {
                "queued": 0,
                "results": [
                    _refused(
                        "a.pdf", "OUTPUT_MISSING", "The document has no extraction"
                    )
                ],
            },
        )

        result = client.document.reprocess("a.pdf", step="assessment")

        assert result.queued is False
        assert result.status == "REFUSED"
        assert result.reason == "OUTPUT_MISSING"
        assert result.message == "The document has no extraction"
        assert result.start_step is None

    def test_an_answer_with_no_result_for_the_document_is_an_error(
        self, client, resolver
    ):
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "beginning"},
            {"queued": 0, "results": []},
        )

        with pytest.raises(IDPProcessingError, match="no result for document a.pdf"):
            client.document.reprocess("a.pdf")

    @pytest.mark.parametrize("entry", [{"status": "QUEUED"}, "a.pdf", None])
    def test_a_result_that_names_no_document_is_an_error(self, client, resolver, entry):
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "beginning"},
            {"queued": 1, "results": [entry]},
        )

        with pytest.raises(IDPProcessingError, match="cannot be read"):
            client.document.reprocess("a.pdf")

    @pytest.mark.parametrize("answer", [True, None, {"queued": 1}, {"results": "x"}])
    def test_an_answer_that_is_not_a_result_object_is_an_error(
        self, client, resolver, answer
    ):
        """A stack whose resolver answers the earlier ``true`` is not read as queued."""
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "beginning"},
            answer,
        )

        with pytest.raises(IDPProcessingError, match="no results"):
            client.document.reprocess("a.pdf")


# ------------------------------------------------------------------ what is refused before a request


class TestValidation:
    @pytest.mark.parametrize("step", ["nonsense", "", "apply_review_changes", "OCR"])
    def test_an_unknown_step_is_refused_before_any_request(self, stack, client, step):
        with pytest.raises(IDPValidationError, match="not a step"):
            client.document.reprocess("a.pdf", step=step)
        assert stack.lookups == []

    @pytest.mark.parametrize("skip", [["extraction"], ["beginning"], ["nonsense"]])
    def test_a_step_that_cannot_be_skipped_is_refused_before_any_request(
        self, stack, client, skip
    ):
        with pytest.raises(IDPValidationError, match="cannot be skipped"):
            client.batch.reprocess("extraction", document_ids=["a.pdf"], skip=skip)
        assert stack.lookups == []

    def test_one_skipped_step_may_be_given_as_a_string(self, client, resolver):
        resolver.answers(
            "reprocessDocument",
            {
                "objectKeys": ["a.pdf"],
                "startStep": "extraction",
                "skipSteps": ["assessment"],
            },
            {"queued": 1, "results": [_queued("a.pdf")]},
        )

        client.document.reprocess("a.pdf", step="extraction", skip="assessment")

        resolver.done()

    def test_every_rerun_step_is_a_step_the_request_accepts(self):
        for step in RerunStep:
            assert server.step_value(step) == step.value

    def test_neither_documents_nor_a_batch_is_a_configuration_error(self, client):
        with pytest.raises(IDPConfigurationError, match="document_ids or batch_id"):
            client.batch.reprocess(step="extraction")

    def test_the_errors_are_sdk_errors(self):
        assert issubclass(ReprocessRefusedError, IDPError)
        assert issubclass(resolver_invoke.ResolverFunctionError, IDPProcessingError)


# ------------------------------------------------------------------ the stack


class TestTheStack:
    def test_a_stack_without_the_output_says_it_has_to_be_updated(self, stack, client):
        stack.outputs = {}

        with pytest.raises(IDPResourceNotFoundError) as refused:
            client.document.reprocess("a.pdf", step="extraction")

        message = str(refused.value)
        assert STACK in message
        assert server.REPROCESS_OUTPUT in message
        assert "after release 0.6.15" in message
        assert "update the stack" in message

    def test_the_older_nested_stack_name_is_tried(self, stack, client, resolver):
        stack.outputs = {}
        stack.has(server.REPROCESS_OUTPUT, pattern="appsync")
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "beginning"},
            {"queued": 1, "results": [_queued("a.pdf", "beginning")]},
        )

        assert client.document.reprocess("a.pdf").queued is True
        assert [pattern for _, pattern, _ in stack.lookups] == [
            "apiresolver",
            "appsync",
        ]

    def test_an_empty_output_is_a_missing_output(self, stack, client):
        stack.outputs = {("apiresolver", server.REPROCESS_OUTPUT): ""}

        with pytest.raises(IDPResourceNotFoundError, match=server.REPROCESS_OUTPUT):
            client.document.reprocess("a.pdf")

    def test_the_function_is_looked_up_once_for_several_requests(
        self, stack, client, resolver
    ):
        for key in ("a.pdf", "b.pdf"):
            resolver.answers(
                "reprocessDocument",
                {"objectKeys": [key], "startStep": "beginning"},
                {"queued": 1, "results": [_queued(key, "beginning")]},
            )

        client.document.reprocess("a.pdf")
        client.document.reprocess("b.pdf")

        assert len(stack.lookups) == 1

    def test_another_stack_is_looked_up_separately(self, stack, client, resolver):
        for _ in range(2):
            resolver.answers(
                "reprocessDocument",
                {"objectKeys": ["a.pdf"], "startStep": "beginning"},
                {"queued": 1, "results": [_queued("a.pdf", "beginning")]},
            )

        client.document.reprocess("a.pdf")
        client.document.reprocess("a.pdf", stack_name="another-stack")

        assert [stack for stack, _, _ in stack.lookups] == [
            STACK,
            "another-stack",
        ]

    def test_a_failed_invoke_is_a_processing_error_and_the_function_is_looked_up_again(
        self, stack, client, resolver
    ):
        resolver.stubber.add_client_error(
            "invoke", "ResourceNotFoundException", "Function not found"
        )
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "beginning"},
            {"queued": 1, "results": [_queued("a.pdf", "beginning")]},
        )

        with pytest.raises(IDPProcessingError, match="Could not invoke"):
            client.document.reprocess("a.pdf")
        assert client.document.reprocess("a.pdf").queued is True

        assert len(stack.lookups) == 2

    def test_the_client_waits_longer_than_the_resolver_may_run(self, monkeypatch):
        """A client that gives up before the function does retries the request,
        and the retry queues the documents a second time."""
        made = {}

        def make(service, region_name=None, config=None):
            made.update(service=service, region=region_name, config=config)
            return object()

        monkeypatch.setattr(resolver_invoke.boto3, "client", make)

        resolver_invoke._lambda_client(REGION)

        assert (made["service"], made["region"]) == ("lambda", REGION)
        assert made["config"].read_timeout > 60

    def test_with_no_region_given_the_request_goes_where_aws_region_says(
        self, monkeypatch
    ):
        """boto3 does not read ``AWS_REGION``. The Region is worked out the one
        way the SDK does (``idp_sdk.resolve_region``) and passed to the stack
        lookup and to the client, so a shell that sets only it reaches its stack."""
        for name in ("AWS_DEFAULT_REGION", "AWS_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        monkeypatch.setattr(resolver_invoke.boto3, "DEFAULT_SESSION", None)
        asked = {}

        def function_arn(stack_name, region, output_key):
            asked["lookup"] = region
            return "arn:aws:lambda:eu-central-1:123456789012:function:reprocess"

        class _Lambda:
            def invoke(self, **_kwargs):
                return {"Payload": io.BytesIO(b'{"queued": 0, "results": []}')}

        def lambda_client(region):
            asked["client"] = region
            return _Lambda()

        monkeypatch.setattr(resolver_invoke, "_function_arn", function_arn)
        monkeypatch.setattr(resolver_invoke, "_lambda_client", lambda_client)

        resolver_invoke.invoke_api_resolver(
            STACK, None, server.REPROCESS_OUTPUT, "reprocessDocument", {}
        )

        assert asked == {"lookup": "eu-central-1", "client": "eu-central-1"}

    def test_with_no_region_anywhere_the_refusal_names_where_one_can_come_from(
        self, monkeypatch
    ):
        for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
        monkeypatch.setattr(resolver_invoke.boto3, "DEFAULT_SESSION", None)

        with pytest.raises(IDPConfigurationError) as refused:
            IDPClient(stack_name=STACK).document.reprocess("a.pdf")

        for source in ("AWS_DEFAULT_REGION", "AWS_REGION", "AWS profile"):
            assert source in str(refused.value)

    def test_an_answer_that_is_not_json_is_a_processing_error(self, client, resolver):
        resolver.stubber.add_response(
            "invoke",
            {"StatusCode": 200, "Payload": StreamingBody(io.BytesIO(b"<html>"), 6)},
        )

        with pytest.raises(IDPProcessingError, match="not JSON"):
            client.document.reprocess("a.pdf")


# ------------------------------------------------------------------ a refused request


class TestARefusedRequest:
    @pytest.mark.parametrize(
        "error_type",
        [
            "PermissionError",
            "ValueError",
            "UnknownStep",
            "StepNotInMode",
            "RunPlanError",
        ],
    )
    def test_a_refusal_of_the_request_is_raised_as_one(
        self, client, resolver, error_type
    ):
        resolver.raises(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "version": "claims", "startStep": "beginning"},
            error_type,
            "Access denied: version 'claims' is refused for this prefix",
        )

        with pytest.raises(ReprocessRefusedError, match="refused for this prefix") as e:
            client.document.reprocess("a.pdf", version="claims")

        assert e.value.reason == error_type

    def test_a_fault_in_the_function_is_not_called_a_refusal(self, client, resolver):
        resolver.raises(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "beginning"},
            "KeyError",
            "'Sections'",
        )

        with pytest.raises(IDPProcessingError, match="reprocessDocument failed") as e:
            client.document.reprocess("a.pdf")

        assert not isinstance(e.value, ReprocessRefusedError)
        assert isinstance(e.value, resolver_invoke.ResolverFunctionError)
        assert (e.value.error_type, e.value.error_message) == ("KeyError", "'Sections'")

    def test_a_function_error_with_no_body_still_raises(self, client, resolver):
        resolver.respond(
            {
                "info": {"fieldName": "reprocessDocument"},
                "arguments": {"objectKeys": ["a.pdf"], "startStep": "beginning"},
            },
            None,
            function_error="Unhandled",
        )

        with pytest.raises(resolver_invoke.ResolverFunctionError) as e:
            client.document.reprocess("a.pdf")

        assert e.value.error_type == "Error"


# ------------------------------------------------------------------ several documents


def _keys(count: int) -> list:
    return [f"batch/doc-{n:03d}.pdf" for n in range(count)]


class TestSeveralDocuments:
    def test_the_counts_and_the_per_document_results(self, client, resolver):
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf", "b.pdf", "c.pdf"], "startStep": "classification"},
            {
                "queued": 1,
                "results": [
                    _queued("a.pdf", "classification"),
                    _refused("b.pdf", "MALWARE_GATE", "The document was quarantined"),
                    {
                        "objectKey": "c.pdf",
                        "status": "FAILED",
                        "startStep": "classification",
                        "reason": "ClientError",
                        "message": None,
                    },
                ],
            },
        )

        result = client.batch.reprocess(
            RerunStep.CLASSIFICATION, document_ids=["a.pdf", "b.pdf", "c.pdf"]
        )

        assert isinstance(result, BatchReprocessResult)
        assert (result.documents_queued, result.documents_failed) == (1, 2)
        assert result.step is RerunStep.CLASSIFICATION
        assert [r.status for r in result.results] == ["QUEUED", "REFUSED", "FAILED"]
        assert result.failed_documents == [
            {
                "object_key": "b.pdf",
                "error": "The document was quarantined",
                "status": "REFUSED",
                "reason": "MALWARE_GATE",
            },
            {
                "object_key": "c.pdf",
                "error": "ClientError",
                "status": "FAILED",
                "reason": "ClientError",
            },
        ]

    def test_a_large_set_is_sent_in_requests_of_bounded_size(
        self, stack, client, resolver
    ):
        keys = _keys(server.DOCUMENTS_PER_REQUEST * 2 + 3)
        parts = [
            keys[: server.DOCUMENTS_PER_REQUEST],
            keys[server.DOCUMENTS_PER_REQUEST : server.DOCUMENTS_PER_REQUEST * 2],
            keys[server.DOCUMENTS_PER_REQUEST * 2 :],
        ]
        for part in parts:
            resolver.answers(
                "reprocessDocument",
                {
                    "objectKeys": part,
                    "startStep": "extraction",
                    "skipSteps": ["evaluation"],
                },
                {"queued": len(part), "results": [_queued(key) for key in part]},
            )

        result = client.batch.reprocess(
            "extraction", document_ids=keys, skip=["evaluation"]
        )

        resolver.done()
        assert [len(part) for part in parts] == [25, 25, 3]
        assert result.documents_queued == len(keys)
        assert [r.document_id for r in result.results] == keys
        assert len(stack.lookups) == 1

    def test_a_refusal_of_the_first_request_stops_before_anything_is_queued(
        self, client, resolver
    ):
        keys = _keys(server.DOCUMENTS_PER_REQUEST + 1)
        resolver.raises(
            "reprocessDocument",
            {
                "objectKeys": keys[: server.DOCUMENTS_PER_REQUEST],
                "startStep": "beginning",
            },
            "PermissionError",
            "refused",
        )

        with pytest.raises(ReprocessRefusedError):
            client.batch.reprocess("beginning", document_ids=keys)

        resolver.done()  # the second request was never sent

    @pytest.mark.parametrize(
        "error_type,status", [("PermissionError", "REFUSED"), ("KeyError", "FAILED")]
    )
    def test_a_later_request_that_is_refused_or_fails_is_reported_on_its_documents(
        self, client, resolver, error_type, status
    ):
        """Documents of the first request are already queued, so the call cannot
        be refused as a whole any more: it reports, and sends the rest."""
        size = server.DOCUMENTS_PER_REQUEST
        keys = _keys(size * 2 + 1)
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": keys[:size], "startStep": "beginning"},
            {"queued": size, "results": [_queued(k, "beginning") for k in keys[:size]]},
        )
        resolver.raises(
            "reprocessDocument",
            {"objectKeys": keys[size : size * 2], "startStep": "beginning"},
            error_type,
            "a mapping refuses this prefix",
        )
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": keys[size * 2 :], "startStep": "beginning"},
            {"queued": 1, "results": [_queued(keys[-1], "beginning")]},
        )

        result = client.batch.reprocess("beginning", document_ids=keys)

        resolver.done()
        assert result.documents_queued == size + 1
        assert result.documents_failed == size
        middle = result.results[size : size * 2]
        assert [r.document_id for r in middle] == keys[size : size * 2]
        assert {(r.status, r.reason, r.message, r.queued) for r in middle} == {
            (status, error_type, "a mapping refuses this prefix", False)
        }
        assert result.failed_documents[0]["error"] == "a mapping refuses this prefix"

    def test_a_batch_id_is_expanded_into_its_documents(
        self, client, resolver, monkeypatch
    ):
        asked = []

        def get_document_ids(batch_id, stack_name=None):
            asked.append((batch_id, stack_name))
            return ["b/a.pdf", "b/b.pdf"]

        monkeypatch.setattr(client.batch, "get_document_ids", get_document_ids)
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["b/a.pdf", "b/b.pdf"], "startStep": "classification"},
            {
                "queued": 2,
                "results": [_queued("b/a.pdf", "classification"), _queued("b/b.pdf")],
            },
        )

        result = client.batch.reprocess(step="classification", batch_id="b")

        assert asked == [("b", STACK)]
        assert (result.documents_queued, result.documents_failed) == (2, 0)

    def test_explicit_documents_win_over_a_batch_id(
        self, client, resolver, monkeypatch
    ):
        monkeypatch.setattr(
            client.batch,
            "get_document_ids",
            lambda *a, **k: pytest.fail("the batch was read"),
        )
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["only.pdf"], "startStep": "extraction"},
            {"queued": 1, "results": [_queued("only.pdf")]},
        )

        client.batch.reprocess("extraction", document_ids=["only.pdf"], batch_id="b")

        resolver.done()

    def test_a_batch_that_does_not_exist_is_not_found(self, client, monkeypatch):
        def missing(batch_id, stack_name=None):
            raise IDPResourceNotFoundError(f"Batch not found: {batch_id}")

        monkeypatch.setattr(client.batch, "get_document_ids", missing)

        with pytest.raises(IDPResourceNotFoundError, match="Batch not found: gone"):
            client.batch.reprocess("extraction", batch_id="gone")


# ------------------------------------------------------------------ options


class TestOptions:
    def test_the_options_of_each_document(self, client, resolver):
        resolver.answers(
            "getReprocessOptions",
            {"objectKeys": ["a.pdf", "b.pdf"], "version": "claims", "revision": 2},
            [
                {
                    "objectKey": "a.pdf",
                    "mode": "pipeline",
                    "defaultStep": "beginning",
                    "steps": [
                        {"id": "beginning", "available": True, "skippable": False},
                        {
                            "id": "assessment",
                            "available": False,
                            "skippable": True,
                            "reason": "OUTPUT_MISSING",
                        },
                    ],
                },
                {"objectKey": "b.pdf", "mode": "bda", "defaultStep": "beginning"},
            ],
        )

        options = client.document.reprocess_options(
            ["a.pdf", "b.pdf"], version="claims", revision=2
        )

        resolver.done()
        assert all(isinstance(option, ReprocessOption) for option in options)
        first, second = options
        assert (first.document_id, first.mode, first.default_step) == (
            "a.pdf",
            "pipeline",
            "beginning",
        )
        assert [(s.id, s.available, s.skippable, s.reason) for s in first.steps] == [
            ("beginning", True, False, None),
            ("assessment", False, True, "OUTPUT_MISSING"),
        ]
        assert (second.mode, second.steps) == ("bda", [])

    def test_one_document_may_be_given_as_a_string(self, client, resolver):
        resolver.answers(
            "getReprocessOptions",
            {"objectKeys": ["a.pdf"]},
            [{"objectKey": "a.pdf", "mode": "pipeline", "defaultStep": "beginning"}],
        )

        assert [o.document_id for o in client.document.reprocess_options("a.pdf")] == [
            "a.pdf"
        ]

    def test_a_refusal_is_raised_as_one(self, client, resolver):
        resolver.raises(
            "getReprocessOptions",
            {"objectKeys": ["a.pdf"], "version": "nope"},
            "PermissionError",
            "Access denied",
        )

        with pytest.raises(ReprocessRefusedError, match="Access denied"):
            client.document.reprocess_options("a.pdf", version="nope")

    def test_a_fault_is_not_called_a_refusal(self, client, resolver):
        resolver.raises(
            "getReprocessOptions", {"objectKeys": ["a.pdf"]}, "KeyError", "x"
        )

        with pytest.raises(resolver_invoke.ResolverFunctionError):
            client.document.reprocess_options("a.pdf")

    def test_an_answer_that_is_not_a_list_is_an_error(self, client, resolver):
        resolver.answers("getReprocessOptions", {"objectKeys": ["a.pdf"]}, {"x": 1})

        with pytest.raises(IDPProcessingError, match="options"):
            client.document.reprocess_options("a.pdf")

    def test_a_large_set_is_read_in_requests_of_bounded_size(self, client, resolver):
        keys = _keys(server.DOCUMENTS_PER_REQUEST + 2)
        for part in (keys[: server.DOCUMENTS_PER_REQUEST], keys[-2:]):
            resolver.answers(
                "getReprocessOptions",
                {"objectKeys": part},
                [
                    {"objectKey": key, "mode": "pipeline", "defaultStep": "beginning"}
                    for key in part
                ],
            )

        options = client.document.reprocess_options(keys)

        resolver.done()
        assert [option.document_id for option in options] == keys


# ------------------------------------------------------------------ an unexpected error


class TestAnUnexpectedError:
    """Anything that is not already an SDK error leaves as ``IDPProcessingError``."""

    def test_from_a_document_reprocess(self, client, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("state machine gone")

        monkeypatch.setattr(server, "reprocess", broken)

        with pytest.raises(IDPProcessingError, match="Reprocess failed: state machine"):
            client.document.reprocess("a.pdf")

    def test_from_a_batch_reprocess(self, client, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("state machine gone")

        monkeypatch.setattr(server, "reprocess", broken)

        with pytest.raises(IDPProcessingError, match="Reprocess failed: state machine"):
            client.batch.reprocess("extraction", document_ids=["a.pdf"])

    def test_from_the_options(self, client, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("no table")

        monkeypatch.setattr(server, "reprocess_options", broken)

        with pytest.raises(IDPProcessingError, match="Reprocess options failed: no"):
            client.document.reprocess_options("a.pdf")


# ------------------------------------------------------------------ the deprecated spelling


class TestRerun:
    def test_document_rerun_warns_and_asks_the_server(self, client, resolver):
        resolver.answers(
            "reprocessDocument",
            {"objectKeys": ["a.pdf"], "startStep": "extraction"},
            {"queued": 1, "results": [_queued("a.pdf")]},
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = client.document.rerun("a.pdf", step="extraction")

        resolver.done()
        assert [w.category for w in caught] == [DeprecationWarning]
        assert "reprocess()" in str(caught[0].message)
        assert result.queued is True

    def test_batch_rerun_warns_and_asks_the_server(self, client, resolver):
        resolver.answers(
            "reprocessDocument",
            {
                "objectKeys": ["a.pdf"],
                "startStep": "extraction",
                "skipSteps": ["summarization"],
            },
            {"queued": 1, "results": [_queued("a.pdf")]},
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = client.batch.rerun(
                step="extraction", document_ids=["a.pdf"], skip=["summarization"]
            )

        resolver.done()
        assert [w.category for w in caught] == [DeprecationWarning]
        assert result.documents_queued == 1
