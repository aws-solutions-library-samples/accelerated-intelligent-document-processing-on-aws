# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the error analyzer's Lambda tool: `retrieve_document_context` and
the Step Functions event-history parsing it depends on.

This is the tool the error-analyzer agent calls first, and everything it does next
is driven by what comes back — `document_found` decides whether analysis proceeds
at all, `primary_failed_function` and `primary_failed_state` are what the agent is
told failed and therefore where it looks, and `lambda_request_ids` are the only
thing that correlates a CloudWatch log line with this document rather than a
concurrent one. So the tests assert the *contents* of the returned dict, not just
that it was returned: a tool that reports `document_found: True` with an empty
request-id list sends the agent to search a log group with no filter, which is how
a diagnosis becomes a guess.

Note that the route from these fields to a log group runs through the agent's own
reasoning, not through code: `cloudwatch_tool` selects log groups from an SSM list
by document status and prioritises request ids using a **different**
`extract_lambda_request_ids` — the X-Ray one — so nothing here is read
programmatically. The tool's docstring is the whole contract, which is why its
wording is part of the change these tests cover.

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
    "LambdaFunctionScheduleFailed": "lambdaFunctionScheduleFailedEventDetails",
    "LambdaFunctionStartFailed": "lambdaFunctionStartFailedEventDetails",
    "TaskSucceeded": "taskSucceededEventDetails",
    "TaskFailed": "taskFailedEventDetails",
    "TaskTimedOut": "taskTimedOutEventDetails",
    "TaskStartFailed": "taskStartFailedEventDetails",
    "TaskSubmitFailed": "taskSubmitFailedEventDetails",
    "ActivityFailed": "activityFailedEventDetails",
    "ActivityScheduleFailed": "activityScheduleFailedEventDetails",
    "ActivityTimedOut": "activityTimedOutEventDetails",
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
            # No detail body: `HistoryEvent` declares no
            # `lambdaFunctionStartedEventDetails` member, so this event genuinely
            # carries nothing beyond its type, id and previousEventId. Emitting one
            # would be the same species of error as #1171 itself, and
            # `TestTheFixtureMatchesTheServiceApiModel` is what caught it here.
            "type": "LambdaFunctionStarted",
            "id": first_id + 2,
            "previousEventId": first_id + 1,
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

    This workflow uses both styles — 15 task states name a function ARN directly and
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


def _schedule_failure_history(
    outcome_type: str = "LambdaFunctionScheduleFailed",
    *,
    detail: dict[str, Any] | None = None,
    attempts: int = 1,
    state_name: str = "OCRStep",
) -> list[dict[str, Any]]:
    """The events a task state emits when the invocation was never scheduled.

    ⚠️ **There is no scheduling event and no `Started` event, because there cannot
    be.** A `*ScheduleFailed` event is emitted *instead of* the corresponding
    `*Scheduled` one, so the only identity in this history is the enclosing state's
    name — the function the state would have invoked appears nowhere.

    Building this with `_direct_chain` instead would place a `Scheduled` **and** a
    `Started` event before a failure *to schedule*, and would then assert a
    capability against a history the service cannot emit. That is the ordering gap
    `TestTheFixtureMatchesTheServiceApiModel` states it cannot close: event sequence
    is not in the API model.

    **Status of the ordering claim.** It is strongly-supported inference, not
    observation: the API reference documents
    `LambdaFunctionScheduleFailedEventDetails` only as "details about a failed Lambda
    function schedule event" and says nothing about sequence, and no live execution
    was observed here — every history in this file is synthesised. What supports it is
    the service model, where `LambdaFunctionScheduledEventDetails.resource` is a
    **required** member, so a scheduling attempt that failed because the resource
    could not be resolved cannot have emitted one.

    `attempts` chains that many failures, which is the shape a `Retry` ladder
    produces: each attempt adds a link and no new state transition.
    """
    events: list[dict[str, Any]] = [
        {
            "type": "TaskStateEntered",
            "id": 1,
            "previousEventId": 0,
            "stateEnteredEventDetails": {"name": state_name, "input": "{}"},
        }
    ]
    for attempt in range(attempts):
        events.append(
            {
                "type": outcome_type,
                "id": 2 + attempt,
                "previousEventId": 1 + attempt,
                _OUTCOME_DETAIL_KEY[outcome_type]: dict(
                    detail
                    or {
                        "error": "Lambda.AccessDeniedException",
                        "cause": "is not authorized to perform: lambda:InvokeFunction",
                    }
                ),
            }
        )
    return events


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
            "failed_states": [],
            "all_request_ids": [],
            "primary_failed_function": None,
            "primary_failed_state": None,
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
        """Documents the shape, and discriminates the FIXTURE rather than the parser.

        This passes against the previous parser too, because the previous parser also
        found nothing in an outcome event carrying no `resource`. What it pins is that
        a failure event on its own is not enough — which is the fact the old fixture
        concealed by injecting a field the service never sends. The parser-level
        discriminator is `test_a_failure_names_the_function_from_its_scheduling_event`.
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
            {"type": "LambdaFunctionStarted", "id": 11, "previousEventId": 10},
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

    # ------------------------------------- the branches that must not raise
    #
    # Every one of these is inside `retrieve_document_context`'s try, so a raise
    # here is returned to the agent as {"document_found": False, "error": ...} and
    # discards every request id and failed-function name already gathered. "Returns
    # nothing" and "raises" are therefore very different outcomes for the same bad
    # input, which is why each malformed shape gets its own case.

    def test_unparseable_task_parameters_name_no_function(self):
        events = _optimized_chain("TaskFailed")
        events[1]["taskScheduledEventDetails"]["parameters"] = "{not json"
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == []

    def test_task_parameters_without_a_function_name_name_no_function(self):
        events = _optimized_chain("TaskFailed")
        events[1]["taskScheduledEventDetails"]["parameters"] = json.dumps(
            {"Payload": {"x": 1}}
        )
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == []

    def test_task_parameters_that_are_not_an_object_name_no_function(self):
        """`parameters` is documented as a JSON object; a JSON *array* parses fine
        and then has no `.get`, so the guard is a type check rather than a parse."""
        events = _optimized_chain("TaskFailed")
        events[1]["taskScheduledEventDetails"]["parameters"] = json.dumps(["OCR"])
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == []

    def test_a_scheduling_detail_that_is_not_an_object_is_skipped(self):
        events = _direct_chain("LambdaFunctionFailed")
        events[1]["lambdaFunctionScheduledEventDetails"] = "unexpected string"
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == []

    def test_a_chain_longer_than_the_hop_bound_gives_up(self):
        """The bound is what stops a broken chain walking back through the whole
        history and attributing a failure to an unrelated earlier invocation. A
        chain padded past it must report nothing rather than eventually finding
        some scheduling event."""
        events = [
            {
                "type": "LambdaFunctionScheduled",
                "id": 1,
                "previousEventId": 0,
                "lambdaFunctionScheduledEventDetails": {"resource": OCR_ARN},
            }
        ]
        # Eight filler links, more than _MAX_CAUSAL_HOPS, each pointing at the last.
        for event_id in range(2, 10):
            events.append(
                {
                    "type": "LambdaFunctionStarted",
                    "id": event_id,
                    "previousEventId": event_id - 1,
                }
            )
        events.append(
            {
                "type": "LambdaFunctionFailed",
                "id": 10,
                "previousEventId": 9,
                "lambdaFunctionFailedEventDetails": {"error": "x"},
            }
        )
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == []

    def test_a_request_id_on_a_top_level_event_field_is_still_mapped(self):
        """The fallback that reads the event's own fields rather than its detail.

        It only fires when the detail yielded no id AND a function name is already
        resolved, so it was unreachable in practice while no function name could be
        resolved at all -- reaching it is a consequence of the fix rather than an
        addition to it.
        """
        events = _direct_chain("LambdaFunctionFailed", detail={"error": "no ids here"})
        events[-1]["traceHeader"] = f"Root=1-abc;RequestId={UUID_A}"
        result = extract_lambda_request_ids(events)
        assert result["function_request_map"] == {"OCRFunction": UUID_A}
        assert result["all_request_ids"] == [UUID_A]

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


@pytest.mark.unit
class TestTheFixtureMatchesTheServiceApiModel:
    """The fixture's shapes are DERIVED from botocore, not hand-written.

    #1171 was a parser reading a field the service does not send, and the reason no
    test caught it was a fixture that sent it anyway. Replacing that fixture with a
    hand-written realistic one fixes the instance; it does not stop the next
    hand-written detail body being wrong in exactly the same way. botocore is already
    a dependency and its Step Functions model is the authority the service is built
    from, so the check is cheap, offline, and needs no network or credentials.

    Two directions, because each catches a different mistake:

    * every key the fixture emits is a real member of that event's detail shape — so
      a typo or an invented field fails here rather than propping up an assertion;
    * ``resource`` is **absent** from the shapes the parser is forbidden to read it
      from — so if AWS ever adds it, this fails and the parser can be simplified
      deliberately rather than the invariant rotting silently.

    ⚠️ **Two limits worth knowing before reading a pass here as "the fixtures are
    right".** It spans the four shared builders — ``_direct_chain``,
    ``_optimized_chain``, ``_state_event`` and ``_schedule_failure_history`` — and not
    the handful of histories written inline elsewhere in this file, which are currently
    valid but unchecked. And it constrains **keys and fields, not sequences**: event
    *ordering* is not in the API model, so a history assembled in an order the service
    cannot emit passes every assertion here. That gap is not hypothetical — a
    schedule-failure case built from ``_direct_chain`` placed a ``Scheduled`` and a
    ``Started`` event before a failure *to schedule*, and asserted a capability the
    parser did not have. It is the same species as the defect this class exists to
    prevent, one layer up. ``_schedule_failure_history`` is the builder that gets that
    ordering right, and what makes it right is the argument in its docstring, not
    anything asserted here.
    """

    @staticmethod
    def _model():
        import botocore.session

        return botocore.session.get_session().get_service_model("stepfunctions")

    @classmethod
    def _shape_for(cls, detail_key: str):
        return cls._model().shape_for(detail_key[0].upper() + detail_key[1:])

    def _plausible_detail(self, outcome_type: str) -> dict[str, str]:
        """A detail body whose keys come from the shape rather than from memory.

        The enumeration below has to fill every outcome type, and hand-writing a
        body per type is the same trap one level up: an earlier version of this
        method passed `error`/`cause` to `LambdaFunctionSucceeded`, which declares
        only `output` and `outputDetails`. Deriving the keys means the enumeration
        cannot drift from the model either.
        """
        shape = self._shape_for(_OUTCOME_DETAIL_KEY[outcome_type])
        return {
            name: "x"
            for name, member in shape.members.items()
            if member.type_name == "string"
        }

    def _every_event_the_fixture_can_build(self):
        events = []
        for outcome in (
            "LambdaFunctionSucceeded",
            "LambdaFunctionFailed",
            "LambdaFunctionTimedOut",
            "LambdaFunctionScheduleFailed",
            "LambdaFunctionStartFailed",
        ):
            events += _direct_chain(outcome, detail=self._plausible_detail(outcome))
        for outcome in (
            "TaskSucceeded",
            "TaskFailed",
            "TaskTimedOut",
            "TaskStartFailed",
            "TaskSubmitFailed",
        ):
            events += _optimized_chain(outcome, detail=self._plausible_detail(outcome))
        for state_type in ("TaskStateEntered", "TaskStateExited"):
            events.append(
                _state_event(
                    state_type, name="S", detail=self._plausible_detail(state_type)
                )
            )
        for schedule_failure in (
            "LambdaFunctionScheduleFailed",
            "ActivityScheduleFailed",
        ):
            events += _schedule_failure_history(
                schedule_failure, detail=self._plausible_detail(schedule_failure)
            )
        return events

    def test_every_detail_key_the_fixture_emits_is_a_member_of_HistoryEvent(self):
        """The key itself, before its contents.

        This is the assertion that earned its keep on first run: the fixture emitted
        `lambdaFunctionStartedEventDetails`, and `HistoryEvent` declares no such
        member — a `LambdaFunctionStarted` event carries no detail at all. Harmless to
        the parser, which never reads that key, and the same species of error as
        #1171: a fixture sending a field the service does not send.
        """
        allowed = set(self._model().shape_for("HistoryEvent").members)
        for event in self._every_event_the_fixture_can_build():
            for key in event:
                assert key in allowed, (
                    f"the fixture emits {key!r}, which HistoryEvent does not declare"
                )

    def test_every_field_the_fixture_emits_is_a_real_member_of_its_shape(self):
        checked = 0
        for event in self._every_event_the_fixture_can_build():
            for key, body in event.items():
                if not key.endswith("EventDetails"):
                    continue
                shape = self._shape_for(key)
                unknown = set(body) - set(shape.members)
                assert not unknown, (
                    f"{key} in the fixture carries {sorted(unknown)}, which "
                    f"{shape.name} does not declare. Sending a field the service "
                    "never sends is how #1171 stayed invisible."
                )
                checked += 1
        assert checked >= 12, (
            f"only {checked} detail bodies were checked; the fixture builders have "
            "changed shape and this assertion is no longer spanning them"
        )

    @pytest.mark.parametrize(
        "detail_key",
        [
            "lambdaFunctionSucceededEventDetails",
            "lambdaFunctionFailedEventDetails",
            "lambdaFunctionTimedOutEventDetails",
            "lambdaFunctionScheduleFailedEventDetails",
            "lambdaFunctionStartFailedEventDetails",
            "stateEnteredEventDetails",
            "stateExitedEventDetails",
        ],
    )
    def test_resource_is_absent_from_the_shapes_the_parser_must_not_read_it_from(
        self, detail_key
    ):
        """The #1171 invariant, derived instead of asserted in prose.

        The parser used to read `resource` off exactly these shapes. None declares
        it, which is why `failed_functions` was always empty.
        """
        assert "resource" not in self._shape_for(detail_key).members

    def test_resource_IS_declared_on_the_shapes_the_walk_reads(self):
        """The other half: the scheduling events really do carry it, so the fix has
        somewhere to read from. Without this the test above could pass on a tree
        where nothing carries `resource` at all."""
        for detail_key in (
            "lambdaFunctionScheduledEventDetails",
            "taskScheduledEventDetails",
        ):
            assert "resource" in self._shape_for(detail_key).members


@pytest.mark.unit
class TestAStateEventIsNeverResolvedCausally:
    """A `TaskStateEntered` event PRECEDES its own scheduling event.

    So a backwards walk from one can only ever find somebody else's. In a linear
    history the service points a state's `TaskStateEntered` at the **previous
    state's** `TaskStateExited`, which is why this is not a hypothetical: the walk
    leaves the state and lands on the previous task's `LambdaFunctionScheduled`.

    Measured before the fix: a request id carried in `ClassificationStep`'s
    `stateEnteredEventDetails.input` was keyed under `OCRFunction`, and it
    overwrote OCRFunction's own correct request id. That is worse than the previous
    behaviour on this path, which keyed it under the state name.
    """

    @staticmethod
    def _linear_two_state_history():
        """Two consecutive states, chained the way the service chains them."""
        return [
            # --- OCRStep, complete
            {
                "type": "TaskStateEntered",
                "id": 1,
                "previousEventId": 0,
                "stateEnteredEventDetails": {"name": "OCRStep", "input": "{}"},
            },
            {
                "type": "LambdaFunctionScheduled",
                "id": 2,
                "previousEventId": 1,
                "lambdaFunctionScheduledEventDetails": {"resource": OCR_ARN},
            },
            {"type": "LambdaFunctionStarted", "id": 3, "previousEventId": 2},
            {
                "type": "LambdaFunctionSucceeded",
                "id": 4,
                "previousEventId": 3,
                "lambdaFunctionSucceededEventDetails": {
                    "output": json.dumps({"requestId": UUID_A})
                },
            },
            {
                "type": "TaskStateExited",
                "id": 5,
                "previousEventId": 4,
                "stateExitedEventDetails": {"name": "OCRStep", "output": "{}"},
            },
            # --- ClassificationStep: its TaskStateEntered points at the PREVIOUS
            #     state's TaskStateExited, which is what the service does.
            {
                "type": "TaskStateEntered",
                "id": 6,
                "previousEventId": 5,
                "stateEnteredEventDetails": {
                    "name": "ClassificationStep",
                    "input": json.dumps({"requestId": UUID_B}),
                },
            },
        ]

    def test_a_state_events_request_id_is_keyed_under_the_state_not_the_previous_task(
        self,
    ):
        result = extract_lambda_request_ids(self._linear_two_state_history())
        assert result["function_request_map"]["ClassificationStep"] == UUID_B, (
            "the walk left the state and resolved the PREVIOUS task's scheduling "
            "event, so a state's request id was filed under another function"
        )

    def test_it_does_not_overwrite_the_previous_functions_own_request_id(self):
        """The consequence that makes this a regression rather than a cosmetic
        mis-keying: both ids land on one key and the correct one is lost."""
        result = extract_lambda_request_ids(self._linear_two_state_history())
        assert result["function_request_map"]["OCRFunction"] == UUID_A
        assert set(result["all_request_ids"]) == {UUID_A, UUID_B}

    def test_a_state_exited_event_is_keyed_under_its_state_too(self):
        events = self._linear_two_state_history()
        events.append(
            {
                "type": "TaskStateExited",
                "id": 7,
                "previousEventId": 6,
                "stateExitedEventDetails": {
                    "name": "ClassificationStep",
                    "output": json.dumps({"requestId": UUID_B}),
                },
            }
        )
        result = extract_lambda_request_ids(events)
        assert result["function_request_map"]["ClassificationStep"] == UUID_B

    def test_an_outcome_event_in_the_same_history_still_resolves_causally(self):
        """The control: excluding state events from the walk must not disable it for
        the events where walking backwards is the correct direction."""
        events = self._linear_two_state_history()
        events += [
            {
                "type": "LambdaFunctionScheduled",
                "id": 7,
                "previousEventId": 6,
                "lambdaFunctionScheduledEventDetails": {
                    "resource": "arn:aws:lambda:us-east-1:1:function:Classification"
                },
            },
            {
                "type": "LambdaFunctionFailed",
                "id": 8,
                "previousEventId": 7,
                "lambdaFunctionFailedEventDetails": {"error": "Unhandled"},
            },
        ]
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == ["Classification"]


@pytest.mark.unit
class TestTheInvokeTimeFailureFamily:
    """`LambdaFunctionStartFailed` is what a throttle or a permission failure at
    invoke time produces — precisely the errors the workflow's retry ladders
    enumerate, and so the class an operator is most likely to be looking at. None of
    these four types was read, so `failed_functions` stayed empty for all of them
    even though the scheduling predecessor the walk needs was right there.
    """

    def test_a_direct_invocation_that_failed_to_start_names_its_function(self):
        """`LambdaFunctionStartFailed` follows a successful `LambdaFunctionScheduled`,
        so the walk has somewhere to land. This is the throttle case."""
        result = extract_lambda_request_ids(
            _direct_chain(
                "LambdaFunctionStartFailed",
                detail={"error": "Lambda.TooManyRequestsException"},
            )
        )
        assert result["failed_functions"] == ["OCRFunction"]
        assert result["primary_failed_function"] == "OCRFunction"

    def test_a_schedule_failure_names_its_state_since_no_function_is_in_the_history(
        self,
    ):
        """⚠️ **The one invoke-time type that can name no function**, answered with the
        enclosing state instead — the identity such a history does carry.

        A schedule failure is what Step Functions emits *instead of*
        `LambdaFunctionScheduled`, so the function it would have invoked appears nowhere
        in the history and the causal walk has no scheduling event to land on. Before
        this, every field went empty and the operator was directed to no function, no
        state and no log group for the whole class — which is what a
        `lambda:InvokeFunction` denial produces (#1183).

        The state name goes in a **separate** field. `failed_functions` is consumed as
        function names, including for choosing a log group, and `OCRStep` names no log
        group; widening that field would have made every consumer's read of it
        conditional on something it cannot see.
        """
        result = extract_lambda_request_ids(_schedule_failure_history())

        assert result["failed_states"] == ["OCRStep"]
        assert result["primary_failed_state"] == "OCRStep"
        # And the function fields stay empty rather than being fed a state name.
        assert result["failed_functions"] == []
        assert result["primary_failed_function"] is None

    def test_the_schedule_failure_type_is_read_and_classified_as_a_task_attempt(self):
        """The two memberships the behaviour above rests on, pinned directly so that
        dropping either fails here as well as in the behavioural case."""
        from idp_common.agents.error_analyzer.tools.lambda_tool import (
            _OUTCOME_DETAIL_KEYS,
            _TASK_ATTEMPT_FAILURE_EVENTS,
        )

        assert "LambdaFunctionScheduleFailed" in _OUTCOME_DETAIL_KEYS
        assert "LambdaFunctionScheduleFailed" in _TASK_ATTEMPT_FAILURE_EVENTS

    @pytest.mark.parametrize("outcome", ["TaskStartFailed", "TaskSubmitFailed"])
    def test_an_optimized_invocation_that_never_started_names_its_function(
        self, outcome
    ):
        result = extract_lambda_request_ids(
            _optimized_chain(outcome, detail={"error": "Lambda.ServiceException"})
        )
        assert result["failed_functions"] == ["OCRFunction"]

    def test_the_throttle_error_text_still_yields_a_request_id_when_present(self):
        result = extract_lambda_request_ids(
            _direct_chain(
                "LambdaFunctionStartFailed",
                detail={"cause": f"Rate exceeded, RequestId: {UUID_A}"},
            )
        )
        assert result["function_request_map"] == {"OCRFunction": UUID_A}


@pytest.mark.unit
class TestTheWalkBoundsAreRatcheted:
    """The hop bound, pinned so that raising it fails a test.

    Raising `_MAX_CAUSAL_HOPS` to a million used to leave every test green: the only
    broken-chain case was an orphan that stops at the first hop whatever the bound is.

    ⚠️ **A boundary pair derived from the constant cannot pin the constant.** The two
    tests below build chains of `_MAX_CAUSAL_HOPS` and `_MAX_CAUSAL_HOPS + 1` links,
    so they move with it and pass at any value — measured: with the bound at a
    million they still pass, having built a million events to do it. They verify the
    bound behaves as documented *at its edge*, which is worth having, and they are
    not the ratchet. The ratchet is the absolute assertion on the value, plus the
    fixed-length broken-chain case in the class above.
    """

    def test_the_bound_stays_small_enough_to_do_its_job(self):
        """An absolute pin, because the bound's *purpose* depends on its size.

        Real chains are two hops (`Failed -> Started -> Scheduled`). The bound exists
        so a failure whose chain is broken stops at "unknown" rather than walking back
        through the history and attributing itself to an unrelated earlier
        invocation — and a wrong function name is worse than none, because it also
        selects the log group the agent searches. A large bound re-enables exactly
        that, silently, with every other test still green.

        Raising it is a legitimate decision; it is just one that has to be made
        deliberately, which is what failing here forces.
        """
        from idp_common.agents.error_analyzer.tools.lambda_tool import (
            _MAX_CAUSAL_HOPS,
        )

        assert 2 <= _MAX_CAUSAL_HOPS <= 8, (
            f"_MAX_CAUSAL_HOPS is {_MAX_CAUSAL_HOPS}. Two hops is the real chain "
            "length; a bound much larger than that stops bounding anything, and a "
            "bound below two cannot resolve a normal failure at all."
        )

    @staticmethod
    def _chain_with_links(link_count: int):
        """A scheduling event, `link_count` filler links, then a failure.

        The failure is `link_count` hops from the scheduling event, so the pair of
        tests below sit either side of the bound.
        """
        events = [
            {
                "type": "LambdaFunctionScheduled",
                "id": 1,
                "previousEventId": 0,
                "lambdaFunctionScheduledEventDetails": {"resource": OCR_ARN},
            },
        ]
        for event_id in range(2, 2 + link_count - 1):
            events.append(
                {
                    "type": "LambdaFunctionStarted",
                    "id": event_id,
                    "previousEventId": event_id - 1,
                }
            )
        last = 2 + link_count - 1
        events.append(
            {
                "type": "LambdaFunctionFailed",
                "id": last,
                "previousEventId": last - 1,
                "lambdaFunctionFailedEventDetails": {"error": "x"},
            }
        )
        return events

    def test_a_chain_exactly_at_the_bound_still_resolves(self):
        from idp_common.agents.error_analyzer.tools.lambda_tool import (
            _MAX_CAUSAL_HOPS,
        )

        result = extract_lambda_request_ids(self._chain_with_links(_MAX_CAUSAL_HOPS))
        assert result["failed_functions"] == ["OCRFunction"], (
            "a chain within the documented bound must resolve; if this fails the "
            "bound is smaller than the comment claims"
        )

    def test_a_chain_one_hop_past_the_bound_does_not(self):
        from idp_common.agents.error_analyzer.tools.lambda_tool import (
            _MAX_CAUSAL_HOPS,
        )

        result = extract_lambda_request_ids(
            self._chain_with_links(_MAX_CAUSAL_HOPS + 1)
        )
        assert result["failed_functions"] == [], (
            "the walk went past the bound. Raising it is a deliberate decision, and "
            "this pair is what makes raising it visible"
        )

    def test_a_cycle_terminates_at_the_bound_without_finding_anything(self):
        """A cycle in `previousEventId` is terminated by the bound, not by a
        visited-set.

        There was a visited-set here, and removing it changed no test result in
        either direction — with the bound present, no input distinguishes the two. It
        is gone rather than kept behind a test that only appears to cover it.

        ⚠️ **This case does not fail if the bound is removed; it hangs.** Measured:
        replacing `for _ in range(_MAX_CAUSAL_HOPS + 1)` with `while True` makes this
        test loop forever rather than report anything, so in CI it would surface as a
        job timeout rather than as a named failure. Stated plainly because a hang is a
        poor signal and the bound's absolute pin above is the assertion to rely on —
        this case documents *what terminates a cycle*, which is the bound and not a
        visited-set.
        """
        events = [
            {
                "type": "LambdaFunctionScheduled",
                "id": 1,
                "previousEventId": 0,
                "lambdaFunctionScheduledEventDetails": {"resource": OCR_ARN},
            },
            {"type": "LambdaFunctionStarted", "id": 50, "previousEventId": 51},
            {
                "type": "LambdaFunctionFailed",
                "id": 51,
                "previousEventId": 50,
                "lambdaFunctionFailedEventDetails": {"error": "x"},
            },
        ]
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == []


@pytest.mark.unit
class TestTheFailureVocabularyIsDerivedFromTheServiceModel:
    """The parser's failure vocabulary, recomputed from botocore rather than reviewed.

    Two sets in `lambda_tool` decide whether a failure is seen at all, and a
    hand-maintained list is how the invoke-time family came to be missing from both
    (#1171) and how the schedule-failure class stayed unattributed afterwards (#1183).
    So each is stated here as a **rule over the `HistoryEventType` enumeration** and
    compared against the constant: a service that adds a failure type nobody has
    classified fails here rather than being silently unread, and a spelling dropped from
    the constant fails here too.

    botocore is already a dependency of this module and its Step Functions model is the
    authority the service is built from, so the derivation is offline and needs no
    credentials.

    ⚠️ **One gap, in the derivation rather than in the constants.** A type is found to be
    error-bearing by reaching its detail shape through `_detail_key_for`, which assumes
    the `<eventType>EventDetails` naming convention. Every type in the model today that
    has a per-type detail member follows it, and the two that do not — the state
    transitions — share a detail member by design. A future failure type whose detail
    member is named some other way would not register as error-bearing, so nothing here
    would demand it be classified.
    """

    _SCHEDULED_SUFFIX = "Scheduled"
    _SCHEDULE_FAILED_SUFFIX = "ScheduleFailed"

    @staticmethod
    def _model():
        import botocore.session

        return botocore.session.get_session().get_service_model("stepfunctions")

    @classmethod
    def _event_types(cls) -> set[str]:
        return set(cls._model().shape_for("HistoryEventType").enum)

    @staticmethod
    def _detail_key_for(event_type: str) -> str:
        """The detail member a per-type shape would be reached through.

        `LambdaFunctionScheduleFailed` -> `lambdaFunctionScheduleFailedEventDetails`.
        The state-transition types are the exception the model itself makes: they share
        `stateEnteredEventDetails`/`stateExitedEventDetails`, and `HistoryEvent`
        declares no `taskStateEnteredEventDetails` at all.
        """
        return event_type[0].lower() + event_type[1:] + "EventDetails"

    @classmethod
    def _error_bearing_types(cls) -> set[str]:
        """Every event type whose own detail shape declares both `error` and `cause`.

        That pair is what makes an event a report of something having failed; every
        other event type either carries an `output`, carries nothing, or carries
        bookkeeping.
        """
        history_event = cls._model().shape_for("HistoryEvent")
        found = set()
        for event_type in cls._event_types():
            member = history_event.members.get(cls._detail_key_for(event_type))
            if member is not None and {"error", "cause"} <= set(member.members):
                found.add(event_type)
        return found

    @classmethod
    def _schedulable_families(cls) -> set[str]:
        """The prefix of every family that can be *scheduled*.

        Derived, not listed: a family that has a `<Family>Scheduled` event is a family
        whose events describe one attempt at invoking something. Today that is
        `Activity`, `LambdaFunction` and `Task`.
        """
        return {
            event_type[: -len(cls._SCHEDULED_SUFFIX)]
            for event_type in cls._event_types()
            if event_type.endswith(cls._SCHEDULED_SUFFIX)
        }

    @classmethod
    def _task_attempt_failures(cls) -> set[str]:
        families = cls._schedulable_families()
        return {
            event_type
            for event_type in cls._error_bearing_types()
            if any(event_type.startswith(family) for family in families)
        }

    def test_the_parsers_failure_set_is_exactly_the_derived_one(self):
        from idp_common.agents.error_analyzer.tools.lambda_tool import (
            _TASK_ATTEMPT_FAILURE_EVENTS,
        )

        derived = self._task_attempt_failures()
        assert set(_TASK_ATTEMPT_FAILURE_EVENTS) == derived, (
            "the parser's task-attempt failure set has drifted from the service "
            f"model. Missing: {sorted(derived - set(_TASK_ATTEMPT_FAILURE_EVENTS))}; "
            f"unknown to the model: "
            f"{sorted(set(_TASK_ATTEMPT_FAILURE_EVENTS) - derived)}"
        )

    def test_the_families_that_can_be_scheduled_are_the_three_the_parser_reads(self):
        assert self._schedulable_families() == {"Activity", "LambdaFunction", "Task"}

    def test_what_the_second_clause_excludes_and_why(self):
        """Non-vacuity for the "can be scheduled" clause, and the exclusion list.

        Without it the set would pull in five error-bearing types that are not one
        attempt at invoking something. Each is excluded for its own reason, and this
        pins the membership so that a sixth cannot appear unnoticed:

        * the three `Execution*` types end the execution — by the time one arrives a
          caught failure has already entered its handler, so attributing them names the
          handler (#1139);
        * `MapRunFailed` aggregates a distributed Map run rather than one attempt;
        * `EvaluationFailed` is an expression evaluation, and its detail carries its own
          required `state` member, so it needs none of this machinery.
        """
        excluded = self._error_bearing_types() - self._task_attempt_failures()
        assert excluded == {
            "ExecutionFailed",
            "ExecutionAborted",
            "ExecutionTimedOut",
            "MapRunFailed",
            "EvaluationFailed",
        }
        assert (
            "state" in self._model().shape_for("EvaluationFailedEventDetails").members
        )

    def test_the_schedule_failure_class_is_exactly_two_types_and_both_are_read(self):
        """The answer to "what else is in that family", derived rather than recalled.

        A `*ScheduleFailed` event is emitted *instead of* its family's `*Scheduled`
        one, which is what makes its attempt unattributable to a function: the
        enumeration holds the pair for exactly two families.
        """
        from idp_common.agents.error_analyzer.tools.lambda_tool import (
            _OUTCOME_DETAIL_KEYS,
            _TASK_ATTEMPT_FAILURE_EVENTS,
        )

        schedule_failures = {
            event_type
            for event_type in self._event_types()
            if event_type.endswith(self._SCHEDULE_FAILED_SUFFIX)
        }
        assert schedule_failures == {
            "LambdaFunctionScheduleFailed",
            "ActivityScheduleFailed",
        }
        for event_type in schedule_failures:
            sibling = (
                event_type[: -len(self._SCHEDULE_FAILED_SUFFIX)]
                + self._SCHEDULED_SUFFIX
            )
            assert sibling in self._event_types(), (
                f"{event_type} has no {sibling} to be emitted instead of, so the "
                "premise of the state fallback does not hold for it"
            )
            assert event_type in _OUTCOME_DETAIL_KEYS
            assert event_type in _TASK_ATTEMPT_FAILURE_EVENTS

    def test_the_task_family_has_no_schedule_failure_which_is_why_it_still_resolves(
        self,
    ):
        """The exclusion that matters most, stated as the model's own answer.

        `TaskStartFailed` and `TaskSubmitFailed` are invoke-time failures too, and they
        are deliberately **not** part of the class #1183 is about: the enumeration holds
        no `TaskScheduleFailed`, so a `Task`-family attempt has always got as far as
        `TaskScheduled` and the causal walk still finds its function. If AWS ever adds
        one, this fails and the fallback's reach has to be reconsidered.
        """
        assert "TaskScheduleFailed" not in self._event_types()

    def test_every_type_the_parser_reads_maps_to_a_real_detail_member(self):
        from idp_common.agents.error_analyzer.tools.lambda_tool import (
            _OUTCOME_DETAIL_KEYS,
            _RESOURCE_BEARING_EVENTS,
        )

        declared = set(self._model().shape_for("HistoryEvent").members)
        for mapping in (_OUTCOME_DETAIL_KEYS, _RESOURCE_BEARING_EVENTS):
            for event_type, detail_key in mapping.items():
                assert event_type in self._event_types(), (
                    f"{event_type} is not a HistoryEventType"
                )
                assert detail_key in declared, (
                    f"{event_type} is mapped to {detail_key}, which HistoryEvent does "
                    "not declare"
                )

    def test_each_failure_type_is_read_through_its_own_detail_key(self):
        from idp_common.agents.error_analyzer.tools.lambda_tool import (
            _OUTCOME_DETAIL_KEYS,
            _TASK_ATTEMPT_FAILURE_EVENTS,
        )

        for event_type in _TASK_ATTEMPT_FAILURE_EVENTS:
            assert _OUTCOME_DETAIL_KEYS[event_type] == self._detail_key_for(event_type)

    def test_activity_scheduling_is_deliberately_not_read_as_a_function_source(self):
        """A judgement, with the model's answer beside it so it cannot rot quietly.

        `ActivityScheduled` declares a required `resource` exactly as the two the walk
        reads do — but an activity ARN is never a Lambda function name, so adding it
        would make the walk stop at a scheduling event and return nothing, replacing the
        state name an activity failure can otherwise be given with silence.
        """
        from idp_common.agents.error_analyzer.tools.lambda_tool import (
            _RESOURCE_BEARING_EVENTS,
        )

        activity_scheduled = self._model().shape_for("ActivityScheduledEventDetails")
        assert "resource" in activity_scheduled.members
        assert "ActivityScheduled" not in _RESOURCE_BEARING_EVENTS
        for detail_key in _RESOURCE_BEARING_EVENTS.values():
            shape = self._model().shape_for(detail_key[0].upper() + detail_key[1:])
            assert "resource" in shape.members


@pytest.mark.unit
class TestTheStateFallbackIsBoundedByWhatTheChainNames:
    """The boundary of the state-name answer, which is the whole risk in #1183.

    Reaching for the state name is how the cross-attribution fixed in #1171 arose:
    there, state-transition events were resolved causally, and because a
    `TaskStateEntered` event *precedes* its own scheduling event the walk landed on the
    previous task's. So the rule is about **capability, not about a spelling**: a state
    name is given only when the attempt's causal chain reaches no scheduling event *that
    names a function*, and every other shape must still come back with what it came back
    with before.

    ⚠️ **"No scheduling event at all" is the wrong way to say that, and the difference
    is testable.** Two different histories satisfy the rule. One has no scheduling event
    — a `*ScheduleFailed` is emitted instead of it. The other has one that is transparent
    to the walk because its family's scheduling event never names a Lambda function,
    which is the Activity family: `test_an_activity_failure_after_a_successful_schedule`
    below is that case, and a rule phrased as "no scheduling event at all" would forbid
    the answer the code gives it.

    Each case below is an attempt to get a state name reported where one would be wrong,
    or to get the pre-#1183 answer back where a better one is now available.
    """

    def test_an_attempt_that_names_its_function_reports_no_state(self):
        """Disjointness. A state name alongside a function name would read as two
        findings about one failure, and the function name is the better one."""
        result = extract_lambda_request_ids(
            _direct_chain("LambdaFunctionFailed", detail={"error": "Unhandled"})
        )
        assert result["failed_functions"] == ["OCRFunction"]
        assert result["failed_states"] == []
        assert result["primary_failed_state"] is None

    def test_a_scheduling_event_that_named_nothing_usable_still_names_nothing(self):
        """The #1128 guard, and the reason this is not a general fallback.

        A layer ARN in a scheduling event's `resource` contributes no function name on
        purpose — a plausible-looking wrong name is worse than none, because it selects
        the log group the agent searches. The history *did* say which resource was to be
        invoked, in a form this module declines to read, so the walk stops there. Falling
        through to the state would turn every such case into a state-name answer and
        make the fallback general, which is what the issue rules out.
        """
        layer = "arn:aws:lambda:us-east-1:123456789012:layer:SharedDeps:3"
        result = extract_lambda_request_ids(
            _direct_chain("LambdaFunctionFailed", resource=layer, detail={"error": "x"})
        )
        assert result["failed_functions"] == []
        assert result["failed_states"] == []

    def test_a_non_lambda_service_integration_names_nothing_either(self):
        """The same boundary reached the other way.

        An optimized integration that is not `lambda:invoke` has no `FunctionName` in
        its parameters, so its `TaskScheduled` resolves to no function — and it is still
        a scheduling event, so the walk stops. A Bedrock or SQS task failure therefore
        reports exactly what it reported before this change.
        """
        events = _optimized_chain("TaskFailed", detail={"error": "ModelTimeout"})
        events[1]["taskScheduledEventDetails"] = {
            "resource": "invokeModel",
            "resourceType": "bedrock",
            "region": "us-east-1",
            "parameters": json.dumps({"ModelId": "some.model"}),
        }
        result = extract_lambda_request_ids(events)
        assert result["failed_functions"] == []
        assert result["failed_states"] == []

    def test_a_denial_mid_pipeline_no_longer_names_the_previous_function(self):
        """⚠️ **The realistic shape, and the reason this change removes a wrong answer
        rather than filling in a blank one.**

        Every other schedule-failure history in this file begins at the state entry, which
        is the truth only for the first state of an execution or a window that starts
        mid-flight. The service chains a state's `TaskStateEntered` to the **previous
        state's** `TaskStateExited`, so on a real execution the chain continues: entry ->
        exit -> succeeded -> started -> scheduled is five hops, inside the six-hop bound.

        Measured on this history before the walk gained its two state branches:
        `failed_functions == ['OCRFunction']` and `primary_failed_function ==
        'OCRFunction'` — the previous, **successful** function, named as the one that
        failed, which is what an operator is told to go and look at. That is the
        #1171/#1139 cross-attribution, still live for this event type.

        Which branch does what, measured on this history over all four combinations:

        | state-entered | state-exit | answer |
        |---|---|---|
        | present | present | `failed_states=['ClassificationStep']` |
        | absent | present | nothing at all |
        | present | absent | `failed_states=['ClassificationStep']` |
        | absent | absent | `failed_functions=['OCRFunction']` |

        So **either branch alone** suppresses the wrong name — the walk needs to reach
        the previous state's scheduling event, and both terminate it before that — while
        only the **entered** branch supplies the right one. The entered branch is examined
        first, which is why removing the exit branch changes nothing here; that branch
        earns its place on `test_a_state_that_has_already_exited_is_never_named`, a chain
        that reaches an exit *before* any entry.
        """
        completed_step = _direct_chain(
            "LambdaFunctionSucceeded", first_id=2, state_name="OCRStep"
        )
        # `_direct_chain` starts every history at the execution's beginning, so its state
        # entry points at event 0. Here it follows `ExecutionStarted`.
        completed_step[0]["previousEventId"] = 1
        history = [
            {"type": "ExecutionStarted", "id": 1, "previousEventId": 0},
            *completed_step,
            {
                "type": "TaskStateExited",
                "id": 6,
                "previousEventId": 5,
                "stateExitedEventDetails": {"name": "OCRStep", "output": "{}"},
            },
            {
                "type": "TaskStateEntered",
                "id": 7,
                "previousEventId": 6,
                "stateEnteredEventDetails": {
                    "name": "ClassificationStep",
                    "input": "{}",
                },
            },
            {
                "type": "LambdaFunctionScheduleFailed",
                "id": 8,
                "previousEventId": 7,
                "lambdaFunctionScheduleFailedEventDetails": {
                    "error": "Lambda.AccessDeniedException"
                },
            },
        ]

        result = extract_lambda_request_ids(history)

        assert result["failed_states"] == ["ClassificationStep"]
        assert result["failed_functions"] == [], (
            "the denial was attributed to a function; on this history the only "
            "reachable one is OCRFunction, which succeeded"
        )
        assert result["primary_failed_function"] is None

    def test_an_activity_failure_after_a_successful_schedule(self):
        """The second way the chain can name no function, and the one the rule's short
        phrasing gets wrong.

        `ActivityScheduled` carries a required `resource`, but it is an activity ARN and
        never a function name, so the walk passes through it rather than stopping — and
        an activity failure of any kind is answered with its state. There *is* a
        scheduling event in this chain, so "a state name only where no scheduling event
        exists" would forbid this answer. What the code implements is "no scheduling event
        that names a function", and this is the case that distinguishes the two.
        """
        history = [
            {
                "type": "TaskStateEntered",
                "id": 1,
                "previousEventId": 0,
                "stateEnteredEventDetails": {"name": "HumanReviewStep", "input": "{}"},
            },
            {
                "type": "ActivityScheduled",
                "id": 2,
                "previousEventId": 1,
                "activityScheduledEventDetails": {
                    "resource": "arn:aws:states:us-east-1:123456789012:activity:Review"
                },
            },
            {
                "type": "ActivityStarted",
                "id": 3,
                "previousEventId": 2,
                "activityStartedEventDetails": {"workerName": "worker-1"},
            },
            {
                "type": "ActivityFailed",
                "id": 4,
                "previousEventId": 3,
                "activityFailedEventDetails": {"error": "Rejected"},
            },
        ]

        result = extract_lambda_request_ids(history)

        assert result["failed_states"] == ["HumanReviewStep"]
        assert result["failed_functions"] == []

    def test_a_state_that_has_already_exited_is_never_named(self):
        """The walk stops at a state **exit**.

        Its own state's entry is the first state event a real attempt walks back to. A
        chain that reaches an *exit* first has left the state the attempt belonged to, so
        every state from there back completed successfully — naming one is the
        cross-attribution of #1171 and #1139 in a new place. Without this guard the walk
        continues to `EarlierStep`'s entry and reports a step that finished. The
        realistic-history case above is the same guard with the previous state's
        invocation events present, where what comes back instead is a function name.
        """
        events = [
            {
                "type": "TaskStateEntered",
                "id": 1,
                "previousEventId": 0,
                "stateEnteredEventDetails": {"name": "EarlierStep", "input": "{}"},
            },
            {
                "type": "TaskStateExited",
                "id": 2,
                "previousEventId": 1,
                "stateExitedEventDetails": {"name": "EarlierStep", "output": "{}"},
            },
            {
                "type": "LambdaFunctionScheduleFailed",
                "id": 3,
                "previousEventId": 2,
                "lambdaFunctionScheduleFailedEventDetails": {
                    "error": "Lambda.AccessDeniedException"
                },
            },
        ]
        result = extract_lambda_request_ids(events)
        assert result["failed_states"] == [], (
            "a state that had already exited was reported as the failing step"
        )
        assert result["failed_functions"] == []

    def test_a_schedule_failure_with_no_state_in_the_history_names_nothing(self):
        """The honest residual, kept as the answer rather than papered over.

        A window that starts after the state was entered — a truncated page, a history
        read from the failure backwards — holds no identity at all, and the tool reports
        none rather than inventing one.
        """
        history = _schedule_failure_history()[1:]
        result = extract_lambda_request_ids(history)
        assert result["failed_states"] == []
        assert result["primary_failed_state"] is None

    def test_a_retry_ladder_still_names_the_state_once(self):
        """Each retry adds a link and no new state transition, so the last attempt in a
        long ladder sits outside the hop bound. The first attempt is one hop from the
        state entry and answers for all of them, and the name is reported once."""
        result = extract_lambda_request_ids(_schedule_failure_history(attempts=9))
        assert result["failed_states"] == ["OCRStep"]
        assert result["primary_failed_state"] == "OCRStep"

    def test_a_state_entry_beyond_the_hop_bound_is_not_reached(self):
        """The bound applies to this walk too: far enough back, the answer is silence
        rather than a name found by walking through unrelated history."""
        from idp_common.agents.error_analyzer.tools.lambda_tool import _MAX_CAUSAL_HOPS

        history = _schedule_failure_history(attempts=_MAX_CAUSAL_HOPS + 2)
        result = extract_lambda_request_ids([history[0], history[-1]])
        assert result["failed_states"] == []

    def test_an_unnamed_state_entry_reports_nothing_rather_than_an_empty_name(self):
        """`""` in `failed_states` would render as a step with no name, which reads as a
        finding. The rejection is on truthiness at the one use site; a second guard
        inside `_state_named_by` was measured redundant and is not there."""
        history = _schedule_failure_history()
        history[0]["stateEnteredEventDetails"] = {"name": "", "input": "{}"}
        assert extract_lambda_request_ids(history)["failed_states"] == []

    @pytest.mark.parametrize("malformed_index", [0, 1])
    def test_one_malformed_detail_does_not_discard_the_whole_history(
        self, malformed_index
    ):
        """A detail that is not an object, in either kind of event.

        Found while writing the state cases above: a `stateEnteredEventDetails` of
        `"OCRStep"` instead of `{"name": "OCRStep"}` raised `AttributeError` out of
        `extract_lambda_request_ids`, where `retrieve_document_context`'s broad `except`
        turns it into `document_found: False` — so one malformed event took the whole
        document's context with it, request ids included. Nothing the service sends looks
        like this, which is exactly why the failure mode would be a total one and
        unreproducible. The event is skipped instead, and the rest of the history is
        still read: the intact `OCRFunction` failure below is the evidence for "still
        read", since an assertion on the empty field alone would also pass if the whole
        call had been abandoned.
        """
        history = _schedule_failure_history()
        history[malformed_index][
            _OUTCOME_DETAIL_KEY[history[malformed_index]["type"]]
        ] = "OCRStep"
        history += _direct_chain("LambdaFunctionFailed", first_id=20)

        result = extract_lambda_request_ids(history)

        assert result["failed_states"] == []
        assert result["failed_functions"] == ["OCRFunction"]

    def test_an_activity_schedule_failure_is_answered_the_same_way(self):
        """Closure over the derived vocabulary. This solution declares no Activity, so
        the case is synthetic — but `ActivityScheduleFailed` is the other half of the
        class, and an activity's history never carries a function ARN at all."""
        result = extract_lambda_request_ids(
            _schedule_failure_history("ActivityScheduleFailed", state_name="WorkStep")
        )
        assert result["failed_states"] == ["WorkStep"]
        assert result["failed_functions"] == []

    def test_a_schedule_failures_state_name_does_not_reach_the_function_request_map(
        self,
    ):
        """A uuid in a denial's cause is not a Lambda request id — the invocation never
        happened — so a schedule failure contributes no mapping, however its cause reads.

        The claim is scoped to this path on purpose. `function_request_map` is **not**
        exclusively function names: the state-transition path keys a request id found in a
        state's input or output under that state's name, which
        `test_a_state_name_keys_a_request_id_when_no_function_is_resolvable` asserts and
        #1171 chose deliberately. What this pins is that the new state attribution does
        not add to that.
        """
        result = extract_lambda_request_ids(
            _schedule_failure_history(
                detail={"cause": f"AccessDenied, RequestId: {UUID_A}"}
            )
        )
        assert result["failed_states"] == ["OCRStep"]
        assert result["function_request_map"] == {}
        assert "OCRStep" not in result["failed_functions"]

    def test_several_states_that_could_not_invoke_are_reported_in_history_order(self):
        """`failed_states` is ordered, and `primary_failed_state` is the earliest.

        A `Map` over sections whose invoke permission is missing produces one of these
        per iteration, and the order an operator reads them in should be the order they
        happened. The new field therefore dedupes with `dict.fromkeys`;
        `failed_functions` keeps its existing `set` because re-ordering an existing
        output for no defect would change what every failing document reports today.

        ⚠️ **This case alone catches the mutation only probabilistically.** Replacing
        `dict.fromkeys` with `set` was measured red at 8 of 8 `PYTHONHASHSEED` values
        here, and green at 1 of 40 when the sweep was widened — a small set of short
        strings can happen to iterate in insertion order under some seed, and at
        `PYTHONHASHSEED=16` that mutant passes this whole module. The correct code passes
        under every seed, so there is no flakiness in the other direction. The
        deterministic half is
        `test_the_new_field_is_deduped_by_a_construct_that_preserves_order`, which reads
        the source; this case is what says the resulting order is the history's.
        """
        history = _schedule_failure_history(state_name="OCRStep")
        for offset, state in enumerate(
            ("ClassificationStep", "ExtractionStep", "AssessmentStep")
        ):
            entered_id = 10 + offset * 2
            history += [
                {
                    "type": "TaskStateEntered",
                    "id": entered_id,
                    "previousEventId": entered_id - 1,
                    "stateEnteredEventDetails": {"name": state},
                },
                {
                    "type": "LambdaFunctionScheduleFailed",
                    "id": entered_id + 1,
                    "previousEventId": entered_id,
                    "lambdaFunctionScheduleFailedEventDetails": {
                        "error": "Lambda.AccessDeniedException"
                    },
                },
            ]

        result = extract_lambda_request_ids(history)

        assert result["failed_states"] == [
            "OCRStep",
            "ClassificationStep",
            "ExtractionStep",
            "AssessmentStep",
        ]
        assert result["primary_failed_state"] == "OCRStep"

    def test_the_new_field_is_deduped_by_a_construct_that_preserves_order(self):
        """The deterministic half of the ordering claim, asserted against the source.

        No input can reliably distinguish `dict.fromkeys` from `set` here: hash
        randomisation decides whether a four-element set of short strings happens to
        iterate in insertion order, and measured over 40 seeds one of them does. So the
        behavioural case above is a ~97.5% detector and this reads the code instead.

        `failed_functions` is deliberately **not** included: it keeps `set`, because
        re-ordering an output that every failing document already produces would be a
        change with no defect behind it.
        """
        import ast
        import inspect

        from idp_common.agents.error_analyzer.tools import lambda_tool

        tree = ast.parse(inspect.getsource(lambda_tool))
        # Every expression this module assigns to a `failed_states` key. There are two:
        # the parser's result dict, which is where the dedupe happens, and the tool
        # response, which passes the already-deduped list through.
        assigned = [
            ast.unparse(value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and key.value == "failed_states"
        ]
        assert len(assigned) == 2, (
            f"expected the parser's dict and the tool response; found {assigned}"
        )
        assert "list(dict.fromkeys(failed_states))" in assigned, (
            f"`failed_states` is built by {assigned}. An unordered construct there makes "
            "the order an operator reads the failed steps in depend on the hash seed"
        )
        assert not any("set(" in expression for expression in assigned)

    def test_the_tool_passes_both_identities_through_to_the_agent(self):
        """The caller half. `extract_lambda_request_ids` is pure; this is the dict the
        agent actually reads, and a field the parser fills but the tool drops would be
        the same defect one layer up."""
        stream = MagicMock()
        stream.read.return_value = json.dumps(
            {
                "status": "FAILED",
                "processingDetail": {
                    "executionArn": "arn:aws:states:us-east-1:1:execution:sm:abc",
                    "events": _schedule_failure_history(),
                },
                "timing": {"timestamps": {}},
            }
        ).encode("utf-8")
        with (
            patch(
                "idp_common.agents.error_analyzer.tools.lambda_tool.boto3.client"
            ) as client_factory,
            patch.dict("os.environ", {"LOOKUP_FUNCTION_NAME": "stack-DocumentLookup"}),
        ):
            client_factory.return_value.invoke.return_value = {"Payload": stream}
            result = retrieve_document_context("report.pdf")

        assert result["document_found"] is True
        assert result["failed_states"] == ["OCRStep"]
        assert result["primary_failed_state"] == "OCRStep"
        assert result["failed_functions"] == []
        assert result["primary_failed_function"] is None
