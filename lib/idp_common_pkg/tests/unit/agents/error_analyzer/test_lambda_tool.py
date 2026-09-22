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


def _lambda_event(
    event_type: str,
    *,
    resource: str = "arn:aws:lambda:us-east-1:123456789012:function:OCRFunction",
    name: str = "",
    extra: dict[str, Any] | None = None,
):
    """Build one Step Functions history event of the shape the parser reads."""
    detail_key = {
        "LambdaFunctionSucceeded": "lambdaFunctionSucceededEventDetails",
        "LambdaFunctionFailed": "lambdaFunctionFailedEventDetails",
        "LambdaFunctionTimedOut": "lambdaFunctionTimedOutEventDetails",
        "TaskStateEntered": "stateEnteredEventDetails",
        "TaskStateExited": "stateExitedEventDetails",
    }[event_type]
    detail = {"resource": resource, "name": name}
    detail.update(extra or {})
    return {"type": event_type, detail_key: detail}


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
    """extract_lambda_request_ids: event history -> function/request mapping."""

    def test_no_events_gives_empty_everything(self):
        result = extract_lambda_request_ids([])
        assert result == {
            "function_request_map": {},
            "failed_functions": [],
            "all_request_ids": [],
            "primary_failed_function": None,
        }

    def test_a_function_arn_yields_the_function_name_after_the_marker(self):
        result = extract_lambda_request_ids(
            [
                _lambda_event(
                    "LambdaFunctionSucceeded",
                    extra={"output": json.dumps({"requestId": UUID_A})},
                )
            ]
        )
        assert result["function_request_map"] == {"OCRFunction": UUID_A}
        assert result["all_request_ids"] == [UUID_A]

    def test_a_state_name_is_used_when_there_is_no_function_arn(self):
        result = extract_lambda_request_ids(
            [
                _lambda_event(
                    "TaskStateEntered",
                    resource="",
                    name="ClassificationStep",
                    extra={"input": json.dumps({"requestId": UUID_A})},
                )
            ]
        )
        assert result["function_request_map"] == {"ClassificationStep": UUID_A}

    def test_a_request_id_is_found_in_a_plain_string_field_not_only_in_json(self):
        result = extract_lambda_request_ids(
            [
                _lambda_event(
                    "LambdaFunctionFailed",
                    extra={"cause": f"Task timed out, RequestId: {UUID_A}"},
                )
            ]
        )
        assert result["function_request_map"] == {"OCRFunction": UUID_A}

    @pytest.mark.parametrize(
        "event_type", ["LambdaFunctionFailed", "LambdaFunctionTimedOut"]
    )
    def test_a_failure_and_a_timeout_both_record_the_failed_function(self, event_type):
        # A timeout is a failure for this purpose: the agent needs to know which
        # function to look at, and "timed out" is not a different question.
        result = extract_lambda_request_ids([_lambda_event(event_type)])
        assert result["failed_functions"] == ["OCRFunction"]
        assert result["primary_failed_function"] == "OCRFunction"

    def test_a_success_records_no_failure(self):
        result = extract_lambda_request_ids([_lambda_event("LambdaFunctionSucceeded")])
        assert result["failed_functions"] == []
        assert result["primary_failed_function"] is None

    def test_the_first_failure_in_history_order_becomes_the_primary(self):
        # Later failures are usually consequences of the first, so the agent is
        # pointed at the earliest one.
        result = extract_lambda_request_ids(
            [
                _lambda_event(
                    "LambdaFunctionFailed",
                    resource="arn:aws:lambda:us-east-1:1:function:First",
                ),
                _lambda_event(
                    "LambdaFunctionFailed",
                    resource="arn:aws:lambda:us-east-1:1:function:Second",
                ),
            ]
        )
        assert result["primary_failed_function"] == "First"
        assert set(result["failed_functions"]) == {"First", "Second"}

    def test_a_function_failing_twice_is_reported_once(self):
        event = _lambda_event("LambdaFunctionFailed")
        result = extract_lambda_request_ids([event, event])
        assert result["failed_functions"] == ["OCRFunction"]

    def test_the_same_request_id_seen_twice_is_reported_once(self):
        event = _lambda_event(
            "LambdaFunctionSucceeded",
            extra={"output": json.dumps({"requestId": UUID_A})},
        )
        result = extract_lambda_request_ids([event, event])
        assert result["all_request_ids"] == [UUID_A]

    def test_two_functions_keep_their_own_request_ids(self):
        result = extract_lambda_request_ids(
            [
                _lambda_event(
                    "LambdaFunctionSucceeded",
                    resource="arn:aws:lambda:us-east-1:1:function:OCR",
                    extra={"output": json.dumps({"requestId": UUID_A})},
                ),
                _lambda_event(
                    "LambdaFunctionSucceeded",
                    resource="arn:aws:lambda:us-east-1:1:function:Extract",
                    extra={"output": json.dumps({"requestId": UUID_B})},
                ),
            ]
        )
        assert result["function_request_map"] == {"OCR": UUID_A, "Extract": UUID_B}
        assert set(result["all_request_ids"]) == {UUID_A, UUID_B}

    def test_an_event_type_outside_the_handled_set_is_skipped(self):
        result = extract_lambda_request_ids(
            [
                {
                    "type": "ExecutionStarted",
                    "executionStartedEventDetails": {"input": UUID_A},
                }
            ]
        )
        assert result["all_request_ids"] == []

    def test_an_event_with_no_detail_object_is_skipped(self):
        assert (
            extract_lambda_request_ids([{"type": "LambdaFunctionFailed"}])[
                "failed_functions"
            ]
            == []
        )

    def test_a_function_with_no_request_id_is_still_recorded_as_failed(self):
        # The mapping needs both, but the failure is worth knowing on its own --
        # it is what selects the log group to search.
        result = extract_lambda_request_ids(
            [_lambda_event("LambdaFunctionFailed", extra={"cause": "no ids here"})]
        )
        assert result["failed_functions"] == ["OCRFunction"]
        assert result["function_request_map"] == {}

    def test_a_malformed_lambda_arn_is_skipped_rather_than_raising(self):
        # A resource with no ":function:" marker and an empty `name` yields no
        # function name, and the event contributes nothing rather than raising
        # inside `retrieve_document_context`'s try (#1061).
        result = extract_lambda_request_ids(
            [
                _lambda_event(
                    "LambdaFunctionFailed",
                    resource="arn:aws:lambda:us-east-1:123456789012:function",
                )
            ]
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
            [
                _lambda_event(
                    "LambdaFunctionFailed",
                    resource=resource,
                    extra={"cause": f"RequestId: {UUID_A}"},
                )
            ]
        )
        assert result["failed_functions"] == []
        assert result["primary_failed_function"] is None
        assert result["function_request_map"] == {}

    def test_one_unparseable_arn_does_not_discard_the_other_events(self):
        # The distinguishing case, and the reason the severity is not merely "one
        # event is skipped": `extract_lambda_request_ids` is called inside
        # `retrieve_document_context`'s try, so a raise anywhere in the loop is
        # returned as {"document_found": False, "error": ...} and every request id
        # and failed-function name already gathered goes with it. The agent stops
        # there, because document_found is what drives its next step.
        result = extract_lambda_request_ids(
            [
                _lambda_event("LambdaFunctionFailed", extra={"cause": UUID_A}),
                _lambda_event(
                    "LambdaFunctionFailed",
                    resource="arn:aws:lambda:us-east-1:123456789012:function",
                ),
            ]
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
                    "events": [
                        _lambda_event(
                            "LambdaFunctionFailed",
                            extra={"cause": f"RequestId: {UUID_A}"},
                        )
                    ],
                },
                "timing": {"timestamps": {}},
            }
        )
        assert result["document_found"] is True
        assert result["document_status"] == "COMPLETED"
        assert result["execution_arn"].endswith(":abc")
        assert result["lambda_request_ids"] == [UUID_A]
        assert result["primary_failed_function"] == "OCRFunction"
        assert result["execution_events_count"] == 1

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
