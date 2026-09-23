# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the error analyzer's Lambda tool: `retrieve_document_context` and
the Step Functions event-history parsing it depends on.

This is the tool the error-analyzer agent calls first, and everything it does next
is driven by what comes back — `document_found` decides whether analysis proceeds
at all, `primary_failed_function` decides which log group to search, and
`lambda_request_ids` are the only thing that correlates a CloudWatch log line with
this document rather than a concurrent one. So the tests assert the *contents* of
the returned dict, not just that it was returned: a tool that reports
`document_found: True` with an empty request-id list sends the agent to search a
log group with no filter, which is how a diagnosis becomes a guess.

`extract_lambda_request_ids` is pure and gets the bulk of the cases. The boto3
Lambda client is stubbed; nothing here reaches AWS.
"""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from idp_common.agents.error_analyzer.tools.lambda_tool import (
    _extract_request_id_from_json,
    _extract_request_id_from_string,
    extract_lambda_request_ids,
    get_lookup_function_name,
    retrieve_document_context,
)

UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


OCR_ARN = "arn:aws:lambda:us-east-1:123456789012:function:OCRFunction"

_OUTCOME_DETAIL_KEY = {
    "LambdaFunctionSucceeded": "lambdaFunctionSucceededEventDetails",
    "LambdaFunctionFailed": "lambdaFunctionFailedEventDetails",
    "LambdaFunctionTimedOut": "lambdaFunctionTimedOutEventDetails",
    "TaskSucceeded": "taskSucceededEventDetails",
    "TaskFailed": "taskFailedEventDetails",
    "TaskTimedOut": "taskTimedOutEventDetails",
    "TaskStateEntered": "stateEnteredEventDetails",
    "TaskStateExited": "stateExitedEventDetails",
}


def _direct_chain(
    outcome_type: str,
    *,
    resource: str = OCR_ARN,
    detail: dict[str, Any] | None = None,
    first_id: int = 1,
    state_name: str = "OCRStep",
) -> list[dict[str, Any]]:
    """The events Step Functions emits for `Resource: <a function ARN>`.

    ⚠️ **The outcome event carries no `resource` and no `name`, because the real
    ones do not.** ``LambdaFunctionFailedEventDetails`` and
    ``LambdaFunctionTimedOutEventDetails`` hold exactly ``cause`` and ``error`` —
    checked against the Step Functions API model in the installed botocore. The
    function's identity is in ``LambdaFunctionScheduled``, and a reader gets from
    one to the other by following ``previousEventId``.

    The fixture this replaces put ``{"resource": ..., "name": ...}`` into *every*
    detail type under a docstring calling it "the shape the parser reads", so every
    test about function-name extraction ran against a shape the service never emits
    — and `failed_functions` was empty in production while the suite was green
    (#1171). A test double that supplies the very field whose absence is the defect
    blinds coverage, mutation probes and a passing suite simultaneously.
    """
    return [
        {
            "type": "TaskStateEntered",
            "id": first_id,
            "previousEventId": 0,
            "stateEnteredEventDetails": {"name": state_name, "input": "{}"},
        },
        {
            "type": "LambdaFunctionScheduled",
            "id": first_id + 1,
            "previousEventId": first_id,
            "lambdaFunctionScheduledEventDetails": {
                "resource": resource,
                "input": "{}",
            },
        },
        {
            "type": "LambdaFunctionStarted",
            "id": first_id + 2,
            "previousEventId": first_id + 1,
            "lambdaFunctionStartedEventDetails": {},
        },
        {
            "type": outcome_type,
            "id": first_id + 3,
            "previousEventId": first_id + 2,
            _OUTCOME_DETAIL_KEY[outcome_type]: dict(detail or {}),
        },
    ]


def _optimized_chain(
    outcome_type: str,
    *,
    function_name: str = OCR_ARN,
    detail: dict[str, Any] | None = None,
    first_id: int = 1,
    state_name: str = "OCRStep",
) -> list[dict[str, Any]]:
    """The events emitted for `Resource: arn:<partition>:states:::lambda:invoke`.

    This workflow uses both styles — 14 task states name a function ARN directly and
    9 go through the optimized integration — so a parser that reads only the
    ``LambdaFunction*`` family leaves those 9 unattributable. Here the scheduling
    event's ``resource`` is the integration verb, and the target is in
    ``parameters.FunctionName``.
    """
    return [
        {
            "type": "TaskStateEntered",
            "id": first_id,
            "previousEventId": 0,
            "stateEnteredEventDetails": {"name": state_name, "input": "{}"},
        },
        {
            "type": "TaskScheduled",
            "id": first_id + 1,
            "previousEventId": first_id,
            "taskScheduledEventDetails": {
                "resource": "invoke",
                "resourceType": "lambda",
                "region": "us-east-1",
                "parameters": json.dumps({"FunctionName": function_name}),
            },
        },
        {
            "type": "TaskStarted",
            "id": first_id + 2,
            "previousEventId": first_id + 1,
            "taskStartedEventDetails": {"resource": "invoke", "resourceType": "lambda"},
        },
        {
            "type": outcome_type,
            "id": first_id + 3,
            "previousEventId": first_id + 2,
            _OUTCOME_DETAIL_KEY[outcome_type]: dict(detail or {}),
        },
    ]


def _state_event(
    event_type: str,
    *,
    name: str,
    detail: dict[str, Any] | None = None,
    event_id: int = 1,
) -> dict[str, Any]:
    """One `TaskStateEntered`/`TaskStateExited` event, which carries a name and no ARN."""
    body = {"name": name}
    body.update(detail or {})
    return {
        "type": event_type,
        "id": event_id,
        "previousEventId": 0,
        _OUTCOME_DETAIL_KEY[event_type]: body,
    }


@pytest.mark.unit
class TestGetLookupFunctionName:
    """get_lookup_function_name: environment only, and it must not guess."""

    def test_the_environment_variable_is_returned(self, monkeypatch):
        monkeypatch.setenv("LOOKUP_FUNCTION_NAME", "stack-DocumentLookup")
        assert get_lookup_function_name() == "stack-DocumentLookup"

    def test_an_unset_variable_raises_rather_than_inventing_a_name(self, monkeypatch):
        # Guessing a function name from a stack name would invoke whatever happens
        # to exist under that name, in whichever account the role can reach.
        monkeypatch.delenv("LOOKUP_FUNCTION_NAME", raising=False)
        with pytest.raises(ValueError, match="LOOKUP_FUNCTION_NAME"):
            get_lookup_function_name()

    def test_an_empty_variable_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("LOOKUP_FUNCTION_NAME", "")
        with pytest.raises(ValueError, match="LOOKUP_FUNCTION_NAME"):
            get_lookup_function_name()


@pytest.mark.unit
class TestExtractRequestIdFromJson:
    """_extract_request_id_from_json: the five spellings a payload might use."""

    @pytest.mark.parametrize(
        "field",
        ["requestId", "request_id", "RequestId", "awsRequestId", "lambdaRequestId"],
    )
    def test_each_accepted_field_name(self, field):
        assert _extract_request_id_from_json(json.dumps({field: UUID_A})) == UUID_A

    def test_a_non_string_value_is_coerced(self):
        assert (
            _extract_request_id_from_json(json.dumps({"requestId": 12345})) == "12345"
        )

    def test_an_empty_value_is_not_returned(self):
        assert _extract_request_id_from_json(json.dumps({"requestId": ""})) is None

    def test_an_unrelated_payload_yields_nothing(self):
        assert _extract_request_id_from_json(json.dumps({"status": "ok"})) is None

    @pytest.mark.parametrize("text", ["", "not json at all", "[1, 2, 3]", "null"])
    def test_non_object_or_unparseable_input_yields_nothing(self, text):
        # A JSON array or scalar reaches the `field in data` check with a type that
        # does not support it; that must return None rather than raise.
        assert _extract_request_id_from_json(text) is None

    def test_the_first_matching_field_wins(self):
        payload = json.dumps({"requestId": UUID_A, "awsRequestId": UUID_B})
        assert _extract_request_id_from_json(payload) == UUID_A


@pytest.mark.unit
class TestExtractRequestIdFromString:
    """_extract_request_id_from_string: UUID pattern matching."""

    def test_a_bare_uuid_is_found(self):
        assert _extract_request_id_from_string(UUID_A) == UUID_A

    def test_a_uuid_embedded_in_prose_is_found(self):
        assert (
            _extract_request_id_from_string(f"RequestId: {UUID_A} Duration: 12 ms")
            == UUID_A
        )

    def test_matching_is_case_insensitive(self):
        assert _extract_request_id_from_string(UUID_A.upper()) == UUID_A.upper()

    def test_the_first_uuid_wins_when_several_are_present(self):
        assert _extract_request_id_from_string(f"{UUID_A} then {UUID_B}") == UUID_A

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "no identifiers here",
            "1111111-2222-3333-4444-555555555555",  # 7 hex in the first group
            "11111111-2222-3333-4444-55555555555",  # 11 hex in the last group
            "gggggggg-2222-3333-4444-555555555555",  # not hex
        ],
    )
    def test_text_without_a_well_formed_uuid_yields_nothing(self, text):
        # The length constraints matter: a loose pattern would pick up execution
        # ids and correlate the wrong log stream.
        assert _extract_request_id_from_string(text) is None


@pytest.mark.unit
class TestExtractLambdaRequestIds:
    """extract_lambda_request_ids: event history -> function/request mapping.

    Every case here is built from a realistic event CHAIN rather than from a single
    synthesised event, because the identity of the failing function is not in the
    failure event at all — it is in the scheduling event that precedes it, reached by
    ``previousEventId``. A test that puts a ``resource`` on the failure event is
    testing a shape the service does not emit.
    """

    def test_no_events_gives_empty_everything(self):
        result = extract_lambda_request_ids([])
        assert result == {
            "function_request_map": {},
            "failed_functions": [],
            "all_request_ids": [],
            "primary_failed_function": None,
        }

    # ------------------------------------------------------------ the core defect

    def test_a_failure_names_the_function_from_its_scheduling_event(self):
        """The regression this class exists for. Nothing on the failure event says
        which function it was, so `failed_functions` was always empty in
        production while every test here passed (#1171)."""
        result = extract_lambda_request_ids(
            _direct_chain("LambdaFunctionFailed", detail={"error": "Unhandled"})
        )
        assert result["failed_functions"] == ["OCRFunction"]
        assert result["primary_failed_function"] == "OCRFunction"

    def test_the_failure_event_alone_names_nothing(self):
        """The discriminator, and the shape the old fixture was hiding.

        With only the outcome event — which is all the old fixture ever built,
        plus fields the service does not send — there is nothing to resolve. This
        must report no function rather than inventing one, and it is why the
        chain above is the thing to test against.
        """
        outcome = _direct_chain("LambdaFunctionFailed", detail={"error": "x"})[-1]
        result = extract_lambda_request_ids([outcome])
        assert result["failed_functions"] == []
        assert result["primary_failed_function"] is None

    def test_an_optimized_lambda_invoke_task_is_attributed_too(self):
        """9 of this workflow's task states use `states:::lambda:invoke`, whose
        events are the `Task*` family and whose target is in the scheduling
        event's `parameters`, not in its `resource`."""
        result = extract_lambda_request_ids(
            _optimized_chain("TaskFailed", detail={"error": "Lambda.Unknown"})
        )
        assert result["failed_functions"] == ["OCRFunction"]

    def test_a_bare_function_name_in_task_parameters_is_accepted(self):
        result = extract_lambda_request_ids(
            _optimized_chain("TaskFailed", function_name="OCRFunction")
        )
        assert result["failed_functions"] == ["OCRFunction"]

    @pytest.mark.parametrize("outcome", ["TaskFailed", "TaskTimedOut"])
    def test_a_task_timeout_counts_as_a_failure_like_a_lambda_timeout(self, outcome):
        result = extract_lambda_request_ids(_optimized_chain(outcome))
        assert result["failed_functions"] == ["OCRFunction"]

    # ------------------------------------------------------ the causal walk itself

    def test_attribution_follows_the_chain_not_the_neighbouring_event(self):
        """Two interleaved Map iterations. `ProcessSections` runs at MaxConcurrency
        10 and the shard Map at 5, and iterations share one execution history, so
        the event physically before a failure routinely belongs to a different
        iteration and a different function. Positional attribution gets this
        wrong; following `previousEventId` does not."""
        first = _direct_chain(
            "LambdaFunctionSucceeded",
            resource="arn:aws:lambda:us-east-1:1:function:Extraction",
            first_id=1,
            state_name="ExtractionStep",
        )
        second = _direct_chain(
            "LambdaFunctionFailed",
            resource="arn:aws:lambda:us-east-1:1:function:Assessment",
            detail={"error": "Unhandled"},
            first_id=10,
            state_name="AssessmentStep",
        )
        # Interleaved: the Assessment failure's immediately preceding event belongs
        # to the Extraction iteration.
        interleaved = [
            first[0],
            second[0],
            first[1],
            second[1],
            first[2],
            second[2],
            second[3],
            first[3],
        ]
        result = extract_lambda_request_ids(interleaved)
        assert result["failed_functions"] == ["Assessment"], (
            "attribution read the neighbouring event instead of the causal chain"
        )

    def test_a_retry_is_attributed_to_the_same_function(self):
        """A retried task re-schedules, so the walk lands on that attempt's own
        scheduling event. Same function either way, which is the point: the answer
        must not depend on which attempt failed."""
        events = _direct_chain("LambdaFunctionFailed", detail={"error": "first"})
        events += [
            {
                "type": "LambdaFunctionScheduled",
                "id": 10,
                "previousEventId": 4,
                "lambdaFunctionScheduledEventDetails": {"resource": OCR_ARN},
            },
            {
                "type": "LambdaFunctionStarted",
                "id": 11,
                "previousEventId": 10,
                "lambdaFunctionStartedEventDetails": {},
            },
            {
                "type": "LambdaFunctionFailed",
                "id": 12,
                "previousEventId": 11,
                "lambdaFunctionFailedEventDetails": {"error": "second"},
            },
        ]
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == ["OCRFunction"]
        assert result["primary_failed_function"] == "OCRFunction"

    def test_a_broken_chain_reports_nothing_rather_than_walking_back_forever(self):
        """A failure whose `previousEventId` points at an event that is not in the
        history stops at unknown. A wrong function name is worse than none: it also
        selects the log group the agent goes on to search."""
        orphan = {
            "type": "LambdaFunctionFailed",
            "id": 99,
            "previousEventId": 98,
            "lambdaFunctionFailedEventDetails": {"error": "x"},
        }
        result = extract_lambda_request_ids(
            _direct_chain("LambdaFunctionSucceeded") + [orphan]
        )
        assert result["failed_functions"] == []

    def test_a_self_referential_chain_terminates(self):
        result = extract_lambda_request_ids(
            [
                {
                    "type": "LambdaFunctionFailed",
                    "id": 5,
                    "previousEventId": 5,
                    "lambdaFunctionFailedEventDetails": {"error": "x"},
                }
            ]
        )
        assert result["failed_functions"] == []

    # -------------------------------------------------------------- request ids

    def test_a_request_id_in_a_json_output_field_is_mapped_to_the_function(self):
        result = extract_lambda_request_ids(
            _direct_chain(
                "LambdaFunctionSucceeded",
                detail={"output": json.dumps({"requestId": UUID_A})},
            )
        )
        assert result["function_request_map"]["OCRFunction"] == UUID_A
        assert UUID_A in result["all_request_ids"]

    def test_a_request_id_is_found_in_a_plain_string_field_not_only_in_json(self):
        result = extract_lambda_request_ids(
            _direct_chain(
                "LambdaFunctionFailed",
                detail={"cause": f"Task timed out, RequestId: {UUID_A}"},
            )
        )
        assert result["function_request_map"]["OCRFunction"] == UUID_A

    def test_a_state_name_keys_a_request_id_when_no_function_is_resolvable(self):
        """`TaskStateEntered` carries a state name and no ARN. That is still worth
        keying a request id by, so the fallback stays — but it is a fallback, not
        the primary source it used to be."""
        result = extract_lambda_request_ids(
            [
                _state_event(
                    "TaskStateEntered",
                    name="ClassificationStep",
                    detail={"input": json.dumps({"requestId": UUID_A})},
                )
            ]
        )
        assert result["function_request_map"] == {"ClassificationStep": UUID_A}

    def test_two_functions_keep_their_own_request_ids(self):
        result = extract_lambda_request_ids(
            _direct_chain(
                "LambdaFunctionSucceeded",
                resource="arn:aws:lambda:us-east-1:1:function:OCR",
                detail={"output": json.dumps({"requestId": UUID_A})},
                first_id=1,
            )
            + _direct_chain(
                "LambdaFunctionSucceeded",
                resource="arn:aws:lambda:us-east-1:1:function:Extract",
                detail={"output": json.dumps({"requestId": UUID_B})},
                first_id=10,
            )
        )
        assert result["function_request_map"]["OCR"] == UUID_A
        assert result["function_request_map"]["Extract"] == UUID_B
        assert set(result["all_request_ids"]) >= {UUID_A, UUID_B}

    def test_the_same_request_id_seen_twice_is_reported_once(self):
        chain = _direct_chain(
            "LambdaFunctionSucceeded",
            detail={"output": json.dumps({"requestId": UUID_A})},
        )
        result = extract_lambda_request_ids(chain + chain)
        assert result["all_request_ids"] == [UUID_A]

    # --------------------------------------------------------- failure bookkeeping

    @pytest.mark.parametrize(
        "outcome", ["LambdaFunctionFailed", "LambdaFunctionTimedOut"]
    )
    def test_a_failure_and_a_timeout_both_record_the_failed_function(self, outcome):
        # A timeout is a failure for this purpose: the agent needs to know which
        # function to look at, and "timed out" is not a different question.
        result = extract_lambda_request_ids(_direct_chain(outcome))
        assert result["failed_functions"] == ["OCRFunction"]
        assert result["primary_failed_function"] == "OCRFunction"

    def test_a_success_records_no_failure(self):
        result = extract_lambda_request_ids(_direct_chain("LambdaFunctionSucceeded"))
        assert result["failed_functions"] == []
        assert result["primary_failed_function"] is None

    def test_the_first_failure_in_history_order_becomes_the_primary(self):
        # Later failures are usually consequences of the first, so the agent is
        # pointed at the earliest one.
        result = extract_lambda_request_ids(
            _direct_chain(
                "LambdaFunctionFailed",
                resource="arn:aws:lambda:us-east-1:1:function:First",
                first_id=1,
            )
            + _direct_chain(
                "LambdaFunctionFailed",
                resource="arn:aws:lambda:us-east-1:1:function:Second",
                first_id=10,
            )
        )
        assert result["primary_failed_function"] == "First"
        assert set(result["failed_functions"]) == {"First", "Second"}

    def test_a_function_failing_twice_is_reported_once(self):
        chain = _direct_chain("LambdaFunctionFailed")
        result = extract_lambda_request_ids(chain + chain)
        assert result["failed_functions"] == ["OCRFunction"]

    def test_a_function_with_no_request_id_is_still_recorded_as_failed(self):
        # The mapping needs both, but the failure is worth knowing on its own --
        # it is what selects the log group to search.
        result = extract_lambda_request_ids(
            _direct_chain("LambdaFunctionFailed", detail={"cause": "no ids here"})
        )
        assert result["failed_functions"] == ["OCRFunction"]
        assert result["function_request_map"] == {}

    # ------------------------------------------------- ARNs that name no function

    def test_a_malformed_lambda_arn_is_skipped_rather_than_raising(self):
        # A scheduling resource with no ":function:" marker yields no function name,
        # and the chain contributes nothing rather than raising inside
        # `retrieve_document_context`'s try (#1061).
        result = extract_lambda_request_ids(
            _direct_chain(
                "LambdaFunctionFailed",
                resource="arn:aws:lambda:us-east-1:123456789012:function",
            )
        )
        assert result["failed_functions"] == []

    @pytest.mark.parametrize(
        "resource",
        [
            "arn:aws:lambda:us-east-1:123456789012:layer:mylayer:3",
            f"arn:aws:lambda:us-east-1:123456789012:event-source-mapping:{UUID_B}",
        ],
    )
    def test_a_lambda_arn_that_is_not_a_function_arn_names_no_function(self, resource):
        # These carry seven or more segments with "lambda" in the third, so a
        # positional read of segment six succeeds and returns something plausible --
        # the layer NAME (not its version) or the mapping uuid. Reporting either as
        # the function that failed is worse than reporting nothing, because it also
        # selects the log group the agent goes on to search.
        result = extract_lambda_request_ids(
            _direct_chain(
                "LambdaFunctionFailed",
                resource=resource,
                detail={"cause": f"RequestId: {UUID_A}"},
            )
        )
        assert result["failed_functions"] == []
        assert result["primary_failed_function"] is None

    def test_an_integration_resource_is_not_read_as_a_function_name(self):
        """An optimized task's `resource` is the verb `invoke`. Reading it as a name
        would report `invoke` as the failing function for 9 of this workflow's task
        states."""
        events = _optimized_chain("TaskFailed")
        # Drop the parameters, leaving only resource="invoke" to go on.
        events[1]["taskScheduledEventDetails"].pop("parameters")
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == []

    # ------------------------------------------------------------- robustness

    def test_an_event_type_outside_the_handled_set_is_skipped(self):
        result = extract_lambda_request_ids(
            [
                {
                    "type": "ExecutionStarted",
                    "id": 1,
                    "executionStartedEventDetails": {"input": UUID_A},
                }
            ]
        )
        assert result["all_request_ids"] == []

    def test_an_event_with_no_detail_object_is_skipped(self):
        assert (
            extract_lambda_request_ids([{"type": "LambdaFunctionFailed", "id": 1}])[
                "failed_functions"
            ]
            == []
        )

    def test_an_event_with_no_id_does_not_break_the_index(self):
        """A history event always has an `id`, but the parser is inside
        `retrieve_document_context`'s try: a raise here is returned as
        `document_found: False` and discards every request id already gathered."""
        result = extract_lambda_request_ids(
            _direct_chain("LambdaFunctionFailed")
            + [{"type": "LambdaFunctionFailed", "lambdaFunctionFailedEventDetails": {}}]
        )
        assert result["failed_functions"] == ["OCRFunction"]

    def test_one_unattributable_chain_does_not_discard_the_others(self):
        # The distinguishing case, and the reason the severity is not merely "one
        # event is skipped": `extract_lambda_request_ids` is called inside
        # `retrieve_document_context`'s try, so a raise anywhere in the loop is
        # returned as {"document_found": False, "error": ...} and every request id
        # and failed-function name already gathered goes with it. The agent stops
        # there, because document_found is what drives its next step.
        result = extract_lambda_request_ids(
            _direct_chain("LambdaFunctionFailed", detail={"cause": UUID_A}, first_id=1)
            + _direct_chain(
                "LambdaFunctionFailed",
                resource="arn:aws:lambda:us-east-1:123456789012:function",
                first_id=10,
            )
        )
        assert result["failed_functions"] == ["OCRFunction"]
        assert result["all_request_ids"] == [UUID_A]


def _payload(body: dict[str, Any]) -> dict[str, Any]:
    """A boto3 invoke() response whose Payload reads back as `body`."""
    stream = MagicMock()
    stream.read.return_value = json.dumps(body).encode("utf-8")
    return {"Payload": stream}


@pytest.mark.unit
class TestRetrieveDocumentContext:
    """retrieve_document_context: the tool the agent actually calls."""

    def _invoke(self, body, document_id="report.pdf", monkeypatch_env=True):
        with (
            patch(
                "idp_common.agents.error_analyzer.tools.lambda_tool.boto3.client"
            ) as client_factory,
            patch.dict("os.environ", {"LOOKUP_FUNCTION_NAME": "stack-DocumentLookup"}),
        ):
            client = client_factory.return_value
            client.invoke.return_value = _payload(body)
            result = retrieve_document_context(document_id)
        return result, client

    def test_the_lookup_function_is_invoked_synchronously_with_the_object_key(self):
        # An async invoke would return before the answer exists, and the object key
        # is the only argument the lookup function takes.
        _, client = self._invoke({"status": "COMPLETED"})
        kwargs = client.invoke.call_args.kwargs
        assert kwargs["FunctionName"] == "stack-DocumentLookup"
        assert kwargs["InvocationType"] == "RequestResponse"
        assert json.loads(kwargs["Payload"]) == {"object_key": "report.pdf"}

    def test_a_not_found_status_is_reported_as_not_found_rather_than_an_error(self):
        result, _ = self._invoke({"status": "NOT_FOUND"})
        assert result["success"] is False
        assert result["document_found"] is False
        assert result["document_id"] == "report.pdf"

    def test_an_error_status_carries_the_lookup_functions_own_message(self):
        result, _ = self._invoke({"status": "ERROR", "message": "table unavailable"})
        assert result["error"] == "table unavailable"
        assert result["document_found"] is False

    def test_an_error_status_with_no_message_still_reports_something(self):
        result, _ = self._invoke({"status": "ERROR"})
        assert result["error"]
        assert result["document_found"] is False

    def test_a_successful_lookup_reports_the_execution_and_request_context(self):
        result, _ = self._invoke(
            {
                "status": "COMPLETED",
                "processingDetail": {
                    "executionArn": "arn:aws:states:us-east-1:1:execution:sm:abc",
                    # A realistic chain, because this asserts the two fields the
                    # agent acts on -- `lambda_request_ids` filters the log search
                    # and `primary_failed_function` picks the log group. Against a
                    # single synthesised failure event both come back empty, which
                    # is exactly what production was returning (#1171).
                    "events": _direct_chain(
                        "LambdaFunctionFailed",
                        detail={"cause": f"RequestId: {UUID_A}"},
                    ),
                },
                "timing": {"timestamps": {}},
            }
        )
        assert result["document_found"] is True
        assert result["document_status"] == "COMPLETED"
        assert result["execution_arn"].endswith(":abc")
        assert result["lambda_request_ids"] == [UUID_A]
        assert result["primary_failed_function"] == "OCRFunction"
        # Four: the chain a single task invocation actually produces — state
        # entered, scheduled, started, failed.
        assert result["execution_events_count"] == 4

    def test_the_processing_window_is_parsed_from_the_iso_timestamps(self):
        # These two become the CloudWatch query window, so a Z suffix that
        # datetime.fromisoformat rejects would leave the agent searching all time.
        result, _ = self._invoke(
            {
                "status": "COMPLETED",
                "processingDetail": {"events": []},
                "timing": {
                    "timestamps": {
                        "WorkflowStartTime": "2026-01-01T10:00:00Z",
                        "CompletionTime": "2026-01-01T10:05:00Z",
                    }
                },
            }
        )
        assert result["processing_start_time"].year == 2026
        assert (
            result["processing_end_time"] - result["processing_start_time"]
        ).total_seconds() == 300

    def test_absent_timestamps_leave_the_window_unset_rather_than_guessed(self):
        result, _ = self._invoke(
            {"status": "COMPLETED", "processingDetail": {"events": []}, "timing": {}}
        )
        assert result["processing_start_time"] is None
        assert result["processing_end_time"] is None

    def test_a_document_still_running_has_a_start_but_no_end(self):
        result, _ = self._invoke(
            {
                "status": "RUNNING",
                "processingDetail": {"events": []},
                "timing": {"timestamps": {"WorkflowStartTime": "2026-01-01T10:00:00Z"}},
            }
        )
        assert result["processing_start_time"] is not None
        assert result["processing_end_time"] is None

    def test_the_whole_lookup_payload_is_passed_through_for_the_agent_to_read(self):
        body = {"status": "COMPLETED", "processingDetail": {"events": []}, "extra": 1}
        result, _ = self._invoke(body)
        assert result["lookup_function_response"] == body

    def test_a_boto_failure_becomes_an_error_response_rather_than_propagating(self):
        # The agent framework surfaces a raised exception as a tool crash; an error
        # dict lets the agent report what happened and move on.
        with patch(
            "idp_common.agents.error_analyzer.tools.lambda_tool.boto3.client",
            side_effect=RuntimeError("AccessDenied"),
        ):
            result = retrieve_document_context("report.pdf")
        assert result["success"] is False
        assert result["document_found"] is False
        assert "AccessDenied" in result["error"]

    def test_a_missing_lookup_function_name_becomes_an_error_response(self):
        with (
            patch("idp_common.agents.error_analyzer.tools.lambda_tool.boto3.client"),
            patch.dict("os.environ", {}, clear=True),
        ):
            result = retrieve_document_context("report.pdf")
        assert result["document_found"] is False
        assert "LOOKUP_FUNCTION_NAME" in result["error"]

    def test_an_unparseable_payload_becomes_an_error_response(self):
        with (
            patch(
                "idp_common.agents.error_analyzer.tools.lambda_tool.boto3.client"
            ) as client_factory,
            patch.dict("os.environ", {"LOOKUP_FUNCTION_NAME": "fn"}),
        ):
            stream = MagicMock()
            stream.read.return_value = b"not json"
            client_factory.return_value.invoke.return_value = {"Payload": stream}
            result = retrieve_document_context("report.pdf")
        assert result["document_found"] is False
        assert result["success"] is False
