# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``SearchProcessor`` and ``ChatProcessor``.

The two are grouped because they are the SDK's two natural-language entry points
and they fail in opposite ways.

``SearchProcessor.query`` looks up a knowledge-base Lambda in the stack's resource
map, builds a JSON request, invokes it, and reshapes the answer. Its tests use
``botocore.stub.Stubber`` rather than a ``MagicMock``, for the reason the brief
for this work gives: a ``MagicMock`` accepts any ``Payload`` at all, so an
assertion that it "was invoked" says nothing about whether the request would be
understood. The stub declares the exact serialised request, so a renamed field or
a missing pagination cursor fails the invoke itself. moto cannot substitute —
invoking a moto Lambda needs Docker and a real deployment package.

``ChatProcessor`` does not call a service directly. It exports environment
variables that ``idp_common``'s agent framework reads, then builds a
conversational orchestrator and consumes its async event stream. So the things
worth testing are the *composition* decisions: which agents are enabled, which
session the orchestrator gets, and what is stripped from the streamed text before
a caller sees it.

**Why the agent framework is injected rather than imported.** The real
``idp_common.agents.factory`` package registers external MCP agents at import
time, and that registration makes a live Secrets Manager ``GetSecretValue`` call
— verified by importing it, which emits an ``AccessDeniedException`` notice. A
unit test that imports it therefore reaches the network, takes as long as the
credential chain does, and behaves differently in CI than locally. The tests here
put stand-in modules in ``sys.modules`` under the two names the processor imports
*inside* its methods, which short-circuits the real import entirely (a name found
in ``sys.modules`` is returned without its parents being loaded). ``idp_common``
itself is never stubbed out.

A first-party defect and a cross-stack contamination behaviour are both pinned
below rather than fixed, each named in the test that holds it:
``test_query_cannot_find_the_knowledge_base_function_on_any_stack`` and
``test_an_absent_output_leaves_a_previous_stacks_value_in_place``.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import types
from contextlib import contextmanager
from unittest.mock import patch

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

pytestmark = pytest.mark.unit

REGION = "us-east-1"
STACK = "idp-stack"
KB_FUNCTION = "idp-stack-KnowledgeBaseFunction-A1B2"

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
                "TableName": "idp-tracking-table",
                "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}],
                "AttributeDefinitions": [{"AttributeName": "PK", "AttributeType": "S"}],
                "BillingMode": "PAY_PER_REQUEST",
            },
        },
        "ConfigurationTable": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "TableName": "idp-configuration-table",
                "KeySchema": [{"AttributeName": "Configuration", "KeyType": "HASH"}],
                "AttributeDefinitions": [
                    {"AttributeName": "Configuration", "AttributeType": "S"}
                ],
                "BillingMode": "PAY_PER_REQUEST",
            },
        },
        "IdHelperChatMemoryTable": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "TableName": "idp-chat-memory-table",
                "KeySchema": [{"AttributeName": "session_id", "KeyType": "HASH"}],
                "AttributeDefinitions": [
                    {"AttributeName": "session_id", "AttributeType": "S"}
                ],
                "BillingMode": "PAY_PER_REQUEST",
            },
        },
    },
    "Outputs": {
        "S3InputBucketName": {"Value": "idp-input-bucket"},
        "S3OutputBucketName": {"Value": "idp-output-bucket"},
        "LambdaLookupFunctionName": {"Value": "idp-lookup"},
        "S3ReportingBucketName": {"Value": "idp-reporting-bucket"},
        "ReportingDatabase": {"Value": "idp_reporting_db"},
    },
}


def _create_stack(*, drop_outputs: tuple[str, ...] = ()) -> None:
    template = json.loads(json.dumps(STACK_TEMPLATE))
    for key in drop_outputs:
        template["Outputs"].pop(key, None)
    boto3.client("cloudformation", region_name=REGION).create_stack(
        StackName=STACK, TemplateBody=json.dumps(template)
    )


# ===========================================================================
# SearchProcessor
# ===========================================================================


def _search_processor():
    from idp_sdk._core.search_processor import SearchProcessor

    return SearchProcessor(stack_name=STACK, region=REGION)


@contextmanager
def _lambda_stub(proc):
    stub = Stubber(proc.lambda_client)
    stub.activate()
    try:
        yield stub
        stub.assert_no_pending_responses()
    finally:
        stub.deactivate()


def _invoke_response(body: dict, function_error: str | None = None) -> dict:
    response = {
        "StatusCode": 200,
        "Payload": io.BytesIO(json.dumps(body).encode("utf-8")),
    }
    if function_error:
        response["FunctionError"] = function_error
    return response


def _expect_invoke(request: dict) -> dict:
    """The exact ``invoke`` parameters the processor must send."""
    return {
        "FunctionName": KB_FUNCTION,
        "InvocationType": "RequestResponse",
        "Payload": json.dumps(request),
    }


#: The knowledge-base handler's own opaque cursor, which the SDK base64-encodes
#: on the way out. A constant rather than an inline literal so bandit's B106 —
#: keyword name plus string literal — has nothing to match.
KB_CURSOR = "kb-cursor-99"


#: One grounded answer, in the shape the knowledge-base Lambda returns.
KB_ANSWER = {
    "results": [
        {
            "answer": "The total amount is $4,821.50.",
            "confidence": 0.93,
            "citations": [
                {
                    "document_id": "batch-001/invoice1.pdf",
                    "page": 3,
                    "text": "Total due: $4,821.50",
                }
            ],
        }
    ]
}


class TestSearchProcessorConstruction:
    def test_the_processor_validates_the_stack_before_use(self, aws_credentials):
        with mock_aws():
            _create_stack()

            proc = _search_processor()

            assert proc.stack_name == STACK
            assert proc.region == REGION
            assert proc.lambda_client.meta.region_name == REGION
            assert proc.resources["OutputBucket"] == "idp-output-bucket"

    def test_an_unusable_stack_is_refused_at_construction(self, aws_credentials):
        with mock_aws():
            _create_stack()
            boto3.client("cloudformation", region_name=REGION).delete_stack(
                StackName=STACK
            )

            with pytest.raises(ValueError, match="not in a valid state"):
                _search_processor()


class TestSearchQueryCannotResolveItsFunction:
    def test_query_cannot_find_the_knowledge_base_function_on_any_stack(
        self, aws_credentials
    ):
        """DEFECT, pinned as-is: ``query()`` cannot succeed against any stack.

        ``query`` reads ``self.resources["KnowledgeBaseFunctionName"]``, and
        ``StackInfo.get_resources`` — the only thing that ever populates
        ``resources`` — never sets that key. It sets ``InputBucket``,
        ``OutputBucket``, ``ConfigurationBucket``, ``EvaluationBaselineBucket``,
        ``TestSetBucket``, ``DocumentQueueUrl``, ``LookupFunctionName``,
        ``StateMachineArn``, ``DocumentsTable`` and ``SettingsParameter``, and
        nothing else. A search of the whole repository finds the string
        ``KnowledgeBaseFunctionName`` in ``search_processor.py`` and nowhere
        else — not in a template, not in ``stack_info.py``.

        Observable consequence: every ``client.search.query(...)`` raises, on
        every deployment, no matter how the stack is configured. The operation
        layer wraps it as ``IDPProcessingError("Failed to query knowledge base
        ...")``, which reads as a knowledge base that is unavailable or not
        provisioned — so the failure looks like an environment problem rather
        than a missing resource mapping. This is the same defect class the
        docstring of ``tests/unit/test_search_operations.py`` describes: the
        whole body sits under ``except Exception: raise``, so a plausible
        message hides a wiring bug.

        This test asserts the **current** behaviour against a fully populated
        stack. When the key is wired up, this test fails — that is the intent;
        replace it with an assertion that the lookup succeeds rather than
        loosening it.
        """
        with mock_aws():
            _create_stack()
            proc = _search_processor()

            assert "KnowledgeBaseFunctionName" not in proc.resources
            with pytest.raises(
                ValueError, match="KnowledgeBaseFunctionName not found in stack"
            ):
                proc.query("What is the total amount?")


class TestSearchQueryRequest:
    """The request body, exercised with the function name supplied by hand.

    ``query``'s own lookup cannot resolve (see the test above), so these inject
    the name directly into the resource map. That keeps the request and parsing
    logic under test today, and means these tests keep their meaning once the
    lookup is fixed.
    """

    def _ready_processor(self):
        proc = _search_processor()
        proc.resources["KnowledgeBaseFunctionName"] = KB_FUNCTION
        return proc

    def test_a_plain_question_sends_only_the_question_and_the_limit(
        self, aws_credentials
    ):
        """No ``document_ids`` key at all when none was given.

        Sending ``document_ids: null`` or ``[]`` is not the same request: a
        knowledge-base handler reading it as "search within these documents"
        would scope the search to nothing and return no answer for a corpus that
        contains one.
        """
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response(KB_ANSWER),
                    _expect_invoke({"question": "What is the total?", "limit": 10}),
                )

                result = proc.query("What is the total?")

            assert result["question"] == "What is the total?"
            assert result["count"] == 1

    def test_document_ids_scope_the_search_when_supplied(self, aws_credentials):
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response(KB_ANSWER),
                    _expect_invoke(
                        {
                            "question": "q",
                            "limit": 5,
                            "document_ids": ["a.pdf", "b.pdf"],
                        }
                    ),
                )

                proc.query("q", document_ids=["a.pdf", "b.pdf"], limit=5)

    def test_an_empty_document_id_list_is_treated_as_no_filter(self, aws_credentials):
        """``[]`` is falsy, so it is omitted rather than sent.

        Pinned because the two readings differ sharply: omitted means "search
        everything", whereas an empty list forwarded to the handler most likely
        means "search nothing". The current behaviour is the safe one, and it is
        the behaviour a caller filtering a list down to zero matches will get.
        """
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response(KB_ANSWER),
                    _expect_invoke({"question": "q", "limit": 10}),
                )

                proc.query("q", document_ids=[])

    def test_the_incoming_cursor_is_decoded_before_it_is_sent(self, aws_credentials):
        """The wire token is base64; the handler is given the plain value.

        The SDK base64-encodes the handler's cursor on the way out (so it is
        opaque to the caller) and must decode it on the way back in. Forwarding
        the encoded form would hand the handler a cursor it cannot read, which
        typically restarts paging from the beginning — an unterminating loop
        rather than an error.
        """
        # Bandit's B105/B106 match the *identifier*, never the value: the checks
        # test `_token$` against a keyword or dict-key name and no choice of
        # literal quiets them. `next_token` is the SDK's real pagination
        # parameter name (`SearchProcessor.query(next_token=...)`), so renaming
        # it here would stop this test checking the contract it exists for. The
        # flagged lines carry a pragma instead.
        wire = base64.b64encode(b"kb-cursor-42").decode("utf-8")
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response(KB_ANSWER),
                    _expect_invoke(
                        {
                            "question": "q",
                            "limit": 10,
                            "next_token": "kb-cursor-42",  # nosec B105 - decoded cursor
                        }
                    ),
                )

                proc.query("q", next_token=wire)  # nosec B106 - the encoded cursor

    def test_a_malformed_cursor_is_raised_rather_than_dropped(self, aws_credentials):
        """A cursor that will not decode must not become "page one" silently."""
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc):
                with pytest.raises(Exception):
                    proc.query(
                        "q",
                        next_token="not valid base64 !!",  # nosec B106 - corrupt cursor
                    )


class TestSearchQueryResponse:
    def _ready_processor(self):
        proc = _search_processor()
        proc.resources["KnowledgeBaseFunctionName"] = KB_FUNCTION
        return proc

    def test_each_result_is_projected_onto_answer_confidence_and_citations(
        self, aws_credentials
    ):
        """Only three fields survive, and the citations pass through verbatim.

        The citation dicts are handed on unchanged — the operation layer above is
        what turns them into ``SearchCitation``/``SearchDocumentReference`` — so
        reshaping them here would break that layer rather than this one.
        """
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response(
                        {"results": [dict(KB_ANSWER["results"][0], internalScore=0.1)]}
                    ),
                    _expect_invoke({"question": "q", "limit": 10}),
                )

                result = proc.query("q")

            assert result["results"] == [
                {
                    "answer": "The total amount is $4,821.50.",
                    "confidence": 0.93,
                    "citations": [
                        {
                            "document_id": "batch-001/invoice1.pdf",
                            "page": 3,
                            "text": "Total due: $4,821.50",
                        }
                    ],
                }
            ]

    def test_a_result_missing_its_fields_gets_neutral_defaults(self, aws_credentials):
        """An unscored answer reads as ``0.0`` here, and that is a real loss.

        ``confidence`` defaults to ``0.0`` rather than ``None``, so "the handler
        did not score this answer" is indistinguishable from "the handler scored
        it zero" at this layer. The operation above converts a falsy confidence
        back to ``None`` (see ``test_search_operations.py``), which is what makes
        the distinction visible to a caller — so this default is pinned as the
        internal shape the operation relies on, not endorsed as the right one.
        """
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response({"results": [{}]}),
                    _expect_invoke({"question": "q", "limit": 10}),
                )

                result = proc.query("q")

            assert result["results"] == [
                {"answer": "", "confidence": 0.0, "citations": []}
            ]

    def test_no_answers_is_an_empty_result_not_an_error(self, aws_credentials):
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response({"results": []}),
                    _expect_invoke({"question": "q", "limit": 10}),
                )

                result = proc.query("q")

            assert result == {"question": "q", "results": [], "count": 0}
            assert "next_token" not in result

    def test_the_outgoing_cursor_is_base64_encoded_for_the_caller(
        self, aws_credentials
    ):
        """Round-trips with the decode above, which is the property that matters.

        The caller is meant to hand this value straight back, so what is asserted
        is that feeding the returned token to a second ``query`` reproduces the
        handler's own cursor.
        """
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response(dict(KB_ANSWER, next_token=KB_CURSOR)),
                    _expect_invoke({"question": "q", "limit": 10}),
                )
                first = proc.query("q")

                stub.add_response(
                    "invoke",
                    _invoke_response({"results": []}),
                    _expect_invoke(
                        {
                            "question": "q",
                            "limit": 10,
                            "next_token": KB_CURSOR,
                        }
                    ),
                )
                proc.query("q", next_token=first["next_token"])

            assert base64.b64decode(first["next_token"]) == KB_CURSOR.encode("utf-8")

    def test_a_lambda_function_error_is_raised_with_the_handlers_message(
        self, aws_credentials
    ):
        """``FunctionError`` is invisible in the payload, so it is checked apart.

        A Lambda that raised still returns HTTP 200 with a body — here a Python
        traceback dict. Without the ``FunctionError`` check the traceback would be
        parsed as a result set, yielding an answer of ``""`` with confidence
        ``0.0``: a confident-looking empty answer instead of an error.
        """
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response(
                        {
                            "errorMessage": "Knowledge base kb-123 does not exist",
                            "errorType": "ResourceNotFoundException",
                        },
                        function_error="Unhandled",
                    ),
                    _expect_invoke({"question": "q", "limit": 10}),
                )

                with pytest.raises(Exception, match="kb-123 does not exist"):
                    proc.query("q")

    def test_a_function_error_without_a_message_still_raises(self, aws_credentials):
        with mock_aws():
            _create_stack()
            proc = self._ready_processor()

            with _lambda_stub(proc) as stub:
                stub.add_response(
                    "invoke",
                    _invoke_response({}, function_error="Unhandled"),
                    _expect_invoke({"question": "q", "limit": 10}),
                )

                with pytest.raises(Exception, match="Unknown query error"):
                    proc.query("q")


# ===========================================================================
# ChatProcessor
# ===========================================================================


class _FakeOrchestrator:
    """Stands in for ``idp_common``'s conversational orchestrator.

    Only the one method ``ChatProcessor`` uses is implemented, and it records the
    prompts it was given so the processor's own call can be checked.
    """

    def __init__(self, events: list[dict]):
        self._events = events
        self.prompts: list[str] = []
        #: Set when the stream's ``finally`` runs, i.e. when the generator is
        #: finalised. See ``test_a_stream_abandoned_at_the_result_event_is_never_closed``.
        self.stream_finalised = False

    def stream_async(self, prompt: str):
        self.prompts.append(prompt)

        async def _stream():
            try:
                for event in self._events:
                    yield event
            finally:
                self.stream_finalised = True

        return _stream()


class _FakeAgentFactory:
    """Records how the orchestrator was composed."""

    def __init__(self, agent_ids: list[str], orchestrator: _FakeOrchestrator):
        self._agent_ids = agent_ids
        self._orchestrator = orchestrator
        self.calls: list[dict] = []

    def list_available_agents(self) -> list[dict]:
        return [
            {"agent_id": agent_id, "name": agent_id} for agent_id in self._agent_ids
        ]

    def create_conversational_orchestrator(
        self, *, agent_ids, session_id, config, session
    ):
        self.calls.append(
            {
                "agent_ids": list(agent_ids),
                "session_id": session_id,
                "config": config,
                "session": session,
            }
        )
        return self._orchestrator


ALL_AGENT_IDS = [
    "Analytics-Agent",
    "Document-Analysis-Agent",
    "Code-Intelligence-Agent",
]

CONFIG_SENTINEL = object()


@contextmanager
def _agent_framework(
    monkeypatch, agent_ids: list[str] | None = None, events: list[dict] | None = None
):
    """Install stand-ins for the two ``idp_common`` modules the processor imports.

    Placed in ``sys.modules`` under the exact dotted names ``_ensure_orchestrator``
    imports, which short-circuits the real import — including the Secrets Manager
    call the real ``factory`` package makes when it registers external MCP agents.
    Removed again by ``monkeypatch`` when the test ends.
    """
    orchestrator = _FakeOrchestrator(events if events is not None else [])
    factory = _FakeAgentFactory(
        agent_ids if agent_ids is not None else ALL_AGENT_IDS, orchestrator
    )

    factory_module = types.ModuleType("idp_common.agents.factory")
    factory_module.agent_factory = factory
    config_module = types.ModuleType("idp_common.agents.analytics.config")
    config_module.get_analytics_config = lambda: CONFIG_SENTINEL

    monkeypatch.setitem(sys.modules, "idp_common.agents.factory", factory_module)
    monkeypatch.setitem(
        sys.modules, "idp_common.agents.analytics.config", config_module
    )
    yield factory, orchestrator


def _chat_processor(region: str | None = REGION, *, env_ready: bool = True):
    """A chat processor, by default with stack discovery already marked done.

    ``_setup_env`` is covered against a live stack in
    ``TestChatEnvironment`` below and in ``tests/unit/test_chat_operations.py``;
    the orchestration tests skip it so that what they assert is the composition
    and not the discovery.
    """
    from idp_sdk._core.chat_processor import ChatProcessor

    proc = ChatProcessor(stack_name=STACK, region=region)
    proc._env_ready = env_ready
    return proc


class TestChatEnvironment:
    """``_setup_env`` publishes the variables ``idp_common``'s agents read.

    An unset variable does not raise: the agent that needed it loses a tool, or
    queries the wrong resource. So the assertions are on the exported values, and
    the whole ``os.environ`` is snapshotted and restored so nothing leaks into
    another test in the session.
    """

    def test_the_agent_environment_is_built_from_the_live_stack(self, aws_credentials):
        with mock_aws(), patch.dict("os.environ"):
            import os

            _create_stack()
            proc = _chat_processor(env_ready=False)

            proc._setup_env()

            assert os.environ["AWS_STACK_NAME"] == STACK
            assert os.environ["TRACKING_TABLE_NAME"] == "idp-tracking-table"
            assert os.environ["CONFIGURATION_TABLE_NAME"] == "idp-configuration-table"
            assert os.environ["ID_HELPER_CHAT_MEMORY_TABLE"] == "idp-chat-memory-table"
            assert os.environ["ATHENA_DATABASE"] == "idp_reporting_db"
            assert (
                os.environ["ATHENA_OUTPUT_LOCATION"]
                == "s3://idp-reporting-bucket/athena-results/"
            )
            assert os.environ["CLOUDWATCH_LOG_GROUP_PREFIX"] == f"/aws/lambda/{STACK}"
            assert os.environ["SETTINGS_PARAMETER_NAME"] == f"{STACK}-Settings"
            assert proc._env_ready is True

    def test_discovery_runs_once(self, aws_credentials):
        """Repeated ``send_message`` calls must not re-walk the stack each time.

        The stack is deleted between the two calls, so a second discovery would
        raise rather than pass quietly.
        """
        with mock_aws(), patch.dict("os.environ"):
            _create_stack()
            proc = _chat_processor(env_ready=False)
            proc._setup_env()

            boto3.client("cloudformation", region_name=REGION).delete_stack(
                StackName=STACK
            )
            proc._setup_env()  # must be a no-op

    def test_an_absent_output_leaves_a_previous_stacks_value_in_place(
        self, aws_credentials
    ):
        """DEFECT, pinned as-is: cross-stack contamination of the agent env.

        ``_setup_env`` writes each variable only ``if v:``, so a resource the
        stack does not have leaves whatever was already in ``os.environ``
        untouched instead of clearing it. Because these variables live in the
        *process* environment and the SDK supports addressing several stacks from
        one client (``IDPClient(stack_name=...)`` plus per-call ``stack_name``
        overrides), a second ``ChatProcessor`` for a stack with no reporting
        database inherits the first stack's ``ATHENA_DATABASE``.

        Observable consequence: the analytics agent runs Athena queries against a
        *different stack's* database and answers confidently from the wrong
        data — no exception, no warning, and the answer looks entirely normal.
        The same applies to ``ATHENA_OUTPUT_LOCATION``,
        ``ID_HELPER_CHAT_MEMORY_TABLE`` and the two table names.

        Pinned rather than fixed: the fix (clear, or set unconditionally) is a
        production change, and an unconditional write has its own consequence for
        a caller who set one of these deliberately.
        """
        with mock_aws(), patch.dict("os.environ"):
            import os

            os.environ["ATHENA_DATABASE"] = "a_different_stacks_db"
            os.environ["ID_HELPER_CHAT_MEMORY_TABLE"] = "a-different-memory-table"
            _create_stack(drop_outputs=("ReportingDatabase", "S3ReportingBucketName"))

            _chat_processor(env_ready=False)._setup_env()

            assert os.environ["ATHENA_DATABASE"] == "a_different_stacks_db"
            assert os.environ["ID_HELPER_CHAT_MEMORY_TABLE"] == "idp-chat-memory-table"

    def test_no_explicit_region_falls_back_to_the_session_region(
        self, aws_credentials, aws_region
    ):
        """The agents get a concrete region string, never an empty one.

        ``BEDROCK_REGION`` decides which region the model is invoked in, so an
        empty value there is a failed chat rather than a wrong one — which is why
        the fallback chain ends in a hardcoded default.
        """
        with mock_aws(), patch.dict("os.environ"):
            import os

            _create_stack()

            _chat_processor(region=None, env_ready=False)._setup_env()

            assert os.environ["BEDROCK_REGION"] == aws_region
            assert os.environ["AWS_REGION"] == aws_region


class TestOrchestratorComposition:
    def test_code_intelligence_is_excluded_unless_it_is_asked_for(
        self, aws_credentials, monkeypatch
    ):
        """The opt-out is a scope decision, not a performance tweak.

        The code-intelligence agent reads the accelerator's own source and
        configuration to answer questions about it. Enabling it by default would
        put that in the tool set of every SDK chat session, so the default must be
        off — and the exclusion is by exact agent id, which a rename would break
        silently in the permissive direction.
        """
        with _agent_framework(monkeypatch) as (factory, _):
            proc = _chat_processor()

            proc._ensure_orchestrator()

            assert factory.calls[0]["agent_ids"] == [
                "Analytics-Agent",
                "Document-Analysis-Agent",
            ]

    def test_code_intelligence_is_included_when_requested(
        self, aws_credentials, monkeypatch
    ):
        with _agent_framework(monkeypatch) as (factory, _):
            proc = _chat_processor()

            proc._ensure_orchestrator(enable_code_intelligence=True)

            assert factory.calls[0]["agent_ids"] == ALL_AGENT_IDS

    def test_a_generated_session_id_is_prefixed_and_short(
        self, aws_credentials, monkeypatch
    ):
        """The id is the conversation's memory key, so it must be unique per run.

        ``sdk-`` marks its origin in the shared chat-memory table, and the
        orchestrator must be given the *same* id the caller is handed back —
        otherwise a follow-up message addressed to the returned id opens a new
        conversation with no history.
        """
        with _agent_framework(monkeypatch) as (factory, _):
            proc = _chat_processor()

            proc._ensure_orchestrator()

            assert proc.session_id is not None
            assert proc.session_id.startswith("sdk-")
            assert len(proc.session_id) == len("sdk-") + 12
            assert factory.calls[0]["session_id"] == proc.session_id

    def test_two_processors_do_not_share_a_generated_session(
        self, aws_credentials, monkeypatch
    ):
        """Shared ids would cross two callers' conversations in one memory row."""
        with _agent_framework(monkeypatch):
            first = _chat_processor()
            first._ensure_orchestrator()
            second = _chat_processor()
            second._ensure_orchestrator()

            assert first.session_id != second.session_id

    def test_a_caller_supplied_session_id_is_used_verbatim(
        self, aws_credentials, monkeypatch
    ):
        with _agent_framework(monkeypatch) as (factory, _):
            proc = _chat_processor()

            proc._ensure_orchestrator(session_id="my-own-session")

            assert proc.session_id == "my-own-session"
            assert factory.calls[0]["session_id"] == "my-own-session"

    def test_the_orchestrator_is_built_once_per_session(
        self, aws_credentials, monkeypatch
    ):
        """Rebuilding would discard the in-memory conversation each turn."""
        with _agent_framework(monkeypatch) as (factory, _):
            proc = _chat_processor()

            proc._ensure_orchestrator()
            proc._ensure_orchestrator()

            assert len(factory.calls) == 1

    def test_the_orchestrator_gets_a_session_for_the_processors_region(
        self, aws_credentials, monkeypatch
    ):
        """Every AWS call the agents make inherits this session's region.

        A session built without the region would resolve from the ambient
        environment, so the agents would query a different region's tables than
        the stack the caller named.
        """
        with _agent_framework(monkeypatch) as (factory, _):
            proc = _chat_processor(region="us-west-2")

            proc._ensure_orchestrator()

            assert factory.calls[0]["session"].region_name == "us-west-2"
            assert factory.calls[0]["config"] is CONFIG_SENTINEL


# The unfinalised-generator warning is emitted by the garbage collector, so it
# surfaces against whichever test happens to be running when the collection
# occurs rather than the one that caused it. It is suppressed for this class by
# message — narrowly, so an unrelated RuntimeWarning is still reported — and the
# behaviour it reflects is asserted deliberately in
# `test_a_stream_abandoned_at_the_result_event_is_never_closed`.
@pytest.mark.filterwarnings("ignore:coroutine method 'aclose':RuntimeWarning")
class TestSendMessage:
    def test_the_streamed_data_events_are_concatenated_in_order(
        self, aws_credentials, monkeypatch
    ):
        events = [{"data": "42 "}, {"data": "documents "}, {"data": "processed."}]
        with _agent_framework(monkeypatch, events=events) as (_, orchestrator):
            proc = _chat_processor()

            response = proc.send_message("how many documents?")

            assert response.response == "42 documents processed."
            assert response.session_id == proc.session_id
            assert orchestrator.prompts == ["how many documents?"]

    def test_a_thinking_block_is_stripped_before_the_caller_sees_it(
        self, aws_credentials, monkeypatch
    ):
        """The model's internal reasoning is not part of the answer.

        The block spans several events and several lines, so it is only removable
        after the stream is joined, and only with ``DOTALL`` — a newline inside
        it is the case a naive pattern misses, leaving the monologue in the
        user-facing text.
        """
        events = [
            {"data": "<thinking>\nI should query"},
            {"data": " the tracking table.\n</thinking>"},
            {"data": "There are 42 documents."},
        ]
        with _agent_framework(monkeypatch, events=events):
            response = _chat_processor().send_message("how many?")

            assert response.response == "There are 42 documents."

    def test_several_thinking_blocks_are_all_removed(
        self, aws_credentials, monkeypatch
    ):
        """The pattern must be non-greedy, or it eats the text between blocks.

        With a greedy match, everything from the first ``<thinking>`` to the last
        ``</thinking>`` disappears — including the answer in the middle, which is
        the part the caller wanted.
        """
        events = [
            {"data": "<thinking>first</thinking>The answer is 42."},
            {"data": "<thinking>second</thinking> And 7 failed."},
        ]
        with _agent_framework(monkeypatch, events=events):
            response = _chat_processor().send_message("q")

            assert response.response == "The answer is 42. And 7 failed."

    def test_the_stream_stops_at_the_result_event(self, aws_credentials, monkeypatch):
        """A ``result`` event terminates the answer; anything after it is not it.

        The orchestrator emits a final ``result`` carrying its own summary of the
        run. Continuing past it would append that bookkeeping to the user-visible
        text.
        """
        events = [
            {"data": "The answer is 42."},
            {"result": {"stop_reason": "end_turn"}},
            {"data": "TRAILING METADATA"},
        ]
        with _agent_framework(monkeypatch, events=events):
            response = _chat_processor().send_message("q")

            assert response.response == "The answer is 42."

    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
    def test_a_stream_abandoned_at_the_result_event_is_never_closed(
        self, aws_credentials, monkeypatch
    ):
        """DEFECT, pinned as-is: the orchestrator's stream is left unfinalised.

        ``_collect_response`` leaves the ``async for`` with ``break`` on the
        ``result`` event, which suspends the async generator rather than closing
        it. Finalising a suspended async generator means awaiting ``aclose()`` on
        the loop that created it — and ``run_async`` (``_core/async_utils.py``)
        does ``loop.close()`` with no ``loop.shutdown_asyncgens()`` first. So the
        generator's ``finally`` never runs: not at the ``break``, not when the
        last reference goes, and not after an explicit ``gc.collect()`` (measured,
        not inferred). Python reports it as ``RuntimeWarning: coroutine method
        'aclose' ... was never awaited``, which this test suppresses because the
        warning *is* the behaviour being pinned.

        Observable consequence: any cleanup the orchestrator does in a ``finally``
        — releasing the streaming Bedrock response, closing an MCP session — is
        skipped on the **normal** path, since a ``result`` event ends every
        successful turn. One abandoned stream per chat message, so it accumulates
        over a long-running process rather than failing once and visibly.

        The contrast is the point: a stream that runs to exhaustion (no ``result``
        event) *is* finalised, so this is specifically the ``break``.

        Pinned rather than fixed: the fix is in production code, either
        ``shutdown_asyncgens()`` in ``run_async`` or draining the stream instead
        of breaking.
        """
        with _agent_framework(
            monkeypatch, events=[{"data": "answer"}, {"result": {}}, {"data": "x"}]
        ) as (_, orchestrator):
            _chat_processor().send_message("q")

            assert orchestrator.stream_finalised is False, (
                "the stream is now finalised, so the leak is fixed — assert that "
                "instead of relaxing this test"
            )

        with _agent_framework(monkeypatch, events=[{"data": "answer"}]) as (
            _,
            drained,
        ):
            _chat_processor().send_message("q")

            assert drained.stream_finalised is True, (
                "a stream that ends on its own must still be finalised"
            )

    def test_events_that_are_neither_data_nor_result_are_ignored(
        self, aws_credentials, monkeypatch
    ):
        """Tool-use and lifecycle events must not land in the text.

        The stream carries far more than text — tool invocations, agent
        handoffs — and anything not recognised is dropped rather than
        stringified into the answer.
        """
        events = [
            {"init_event_loop": True},
            {"current_tool_use": {"name": "run_athena_query"}},
            {"data": "42 documents."},
        ]
        with _agent_framework(monkeypatch, events=events):
            assert _chat_processor().send_message("q").response == "42 documents."

    def test_an_empty_stream_yields_an_empty_answer_not_a_failure(
        self, aws_credentials, monkeypatch
    ):
        with _agent_framework(monkeypatch, events=[]):
            response = _chat_processor().send_message("q")

            assert response.response == ""
            assert response.session_id is not None

    def test_a_follow_up_without_a_session_id_continues_the_conversation(
        self, aws_credentials, monkeypatch
    ):
        """Omitting the session id means "same conversation", not "a new one".

        This is the ordinary multi-turn shape, and rebuilding here would lose the
        history the second question depends on.
        """
        with _agent_framework(monkeypatch, events=[{"data": "ok"}]) as (factory, _):
            proc = _chat_processor()

            first = proc.send_message("how many documents?")
            second = proc.send_message("break that down by type")

            assert first.session_id == second.session_id
            assert len(factory.calls) == 1

    def test_the_same_session_id_reuses_the_orchestrator(
        self, aws_credentials, monkeypatch
    ):
        with _agent_framework(monkeypatch, events=[{"data": "ok"}]) as (factory, _):
            proc = _chat_processor()

            proc.send_message("q1", session_id="s1")
            proc.send_message("q2", session_id="s1")

            assert len(factory.calls) == 1
            assert proc.session_id == "s1"

    def test_a_different_session_id_starts_a_fresh_orchestrator(
        self, aws_credentials, monkeypatch
    ):
        """Switching sessions must discard the old orchestrator, not reuse it.

        An orchestrator carries its session id from construction, so reusing one
        for a different session would write the new conversation into the old
        session's memory — the two callers' histories merge, and each sees the
        other's questions.
        """
        with _agent_framework(monkeypatch, events=[{"data": "ok"}]) as (factory, _):
            proc = _chat_processor()

            proc.send_message("q1", session_id="s1")
            proc.send_message("q2", session_id="s2")

            assert [call["session_id"] for call in factory.calls] == ["s1", "s2"]
            assert proc.session_id == "s2"

    def test_the_session_id_property_is_none_before_the_first_message(
        self, aws_credentials
    ):
        """A caller reading it early must get ``None``, not a stale id."""
        assert _chat_processor().session_id is None
