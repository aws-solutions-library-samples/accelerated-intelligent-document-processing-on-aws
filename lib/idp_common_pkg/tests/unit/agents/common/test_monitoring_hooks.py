# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the agent monitoring hook providers' event handling.

Complements `test_monitoring.py`, which covers construction and two message-preview
cases. What is added here is the event surface: the lifecycle callbacks and their
effect on `execution_stats`, the tool-result and message previews, and — the part
that matters most — how a model exception is classified.

Throttling classification is the reason to test this carefully. `AgentMonitor`
decides whether an exception is a throttle by two independent routes: the error
code inside a `botocore` `ClientError`, and a case-insensitive substring scan of
the exception's string form for non-`ClientError` types. Only a throttle invokes
`throttling_callback`, and that callback is what the analytics job uses to tell a
user their request is being rate-limited rather than broken. A misclassification is
silent in both directions: a throttle read as a generic error loses the retry
signal, and a generic error read as a throttle tells a user to wait for something
that will never clear.

`ThrottlingMonitor` classifies by error code only — it has no substring route — so
the two providers disagree about a `ModelThrottledException` raised as a plain
exception. That difference is asserted rather than assumed, because it is invisible
at the call site and both providers are registered on the same agent.

These hooks call `logging` heavily and the stats are the only observable output, so
the assertions are on state and callbacks, not on log text.
"""

import json
import logging
from datetime import datetime
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from idp_common.agents.common.monitoring import (
    AgentMonitor,
    MessageTracker,
    ThrottlingMonitor,
)


def _client_error(code: str, message: str = "slow down"):
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}}, "Converse"
    )


def _event(**attrs):
    event = MagicMock()
    for key, value in attrs.items():
        setattr(event, key, value)
    return event


THROTTLING_CODES = [
    "ThrottlingException",
    "ModelThrottledException",
    "ServiceQuotaExceededException",
    "RequestLimitExceeded",
]


@pytest.mark.unit
class TestAgentMonitorLifecycle:
    """The four core callbacks and what they record in execution_stats."""

    def test_a_fresh_monitor_has_zeroed_counters_and_no_timestamps(self):
        stats = AgentMonitor().execution_stats
        assert stats["messages_added"] == 0
        assert stats["tool_invocations"] == 0
        assert stats["model_invocations"] == 0
        assert stats["requests_processed"] == 0
        assert stats["start_time"] is None
        assert stats["end_time"] is None

    def test_before_invocation_starts_the_clock_and_counts_the_request(self):
        monitor = AgentMonitor()
        monitor.on_before_invocation(_event(agent="a"))
        assert monitor.execution_stats["requests_processed"] == 1
        assert isinstance(monitor.execution_stats["start_time"], datetime)

    def test_several_requests_accumulate(self):
        monitor = AgentMonitor()
        for _ in range(3):
            monitor.on_before_invocation(_event(agent="a"))
        assert monitor.execution_stats["requests_processed"] == 3

    def test_after_invocation_stops_the_clock(self):
        monitor = AgentMonitor()
        monitor.on_before_invocation(_event(agent="a"))
        monitor.on_after_invocation(_event(agent="a"))
        assert (
            monitor.execution_stats["end_time"] >= monitor.execution_stats["start_time"]
        )

    def test_after_invocation_without_a_start_still_completes(self):
        # The hook can fire without its partner when an agent is reused across
        # invocations, and computing a duration from a null start would raise
        # inside a logging path.
        monitor = AgentMonitor()
        monitor.on_after_invocation(_event(agent="a"))
        assert monitor.execution_stats["end_time"] is not None

    def test_agent_initialized_records_nothing_but_does_not_raise(self):
        monitor = AgentMonitor()
        monitor.on_agent_initialized(_event(agent="a"))
        assert monitor.execution_stats["requests_processed"] == 0

    def test_detailed_logging_off_still_records_the_same_stats(self):
        # The flag controls log verbosity only; a monitor with it off must not
        # become a monitor that counts differently.
        quiet = AgentMonitor(enable_detailed_logging=False)
        loud = AgentMonitor(enable_detailed_logging=True)
        for monitor in (quiet, loud):
            monitor.on_before_invocation(_event(agent="a"))
            monitor.on_message_added(_event(message={"role": "user", "content": []}))
        counters = (
            "messages_added",
            "tool_invocations",
            "model_invocations",
            "requests_processed",
        )
        assert {k: quiet.execution_stats[k] for k in counters} == {
            k: loud.execution_stats[k] for k in counters
        }
        assert quiet.execution_stats["messages_added"] == 1

    def test_the_configured_log_level_reaches_the_monitors_own_logger(self):
        monitor = AgentMonitor(log_level=logging.DEBUG)
        assert monitor.monitor_logger.level == logging.DEBUG


@pytest.mark.unit
class TestAgentMonitorMessageAdded:
    """on_message_added: the counter and the history entry."""

    def test_a_message_is_counted_and_recorded(self):
        monitor = AgentMonitor()
        monitor.on_message_added(
            _event(message={"role": "user", "content": [{"text": "hello"}]})
        )
        assert monitor.execution_stats["messages_added"] == 1
        assert len(monitor.message_history) == 1

    def test_each_history_entry_carries_a_timestamp_and_the_role(self):
        monitor = AgentMonitor()
        monitor.on_message_added(
            _event(message={"role": "assistant", "content": [{"text": "hi"}]})
        )
        entry = monitor.message_history[0]
        assert entry["timestamp"]
        assert entry["role"] == "assistant"

    def test_messages_are_recorded_in_order(self):
        monitor = AgentMonitor()
        for role in ("user", "assistant", "user"):
            monitor.on_message_added(_event(message={"role": role, "content": []}))
        assert [e["role"] for e in monitor.message_history] == [
            "user",
            "assistant",
            "user",
        ]

    def test_a_malformed_message_is_recorded_rather_than_dropped(self):
        # Losing a message from the history silently would make the count and the
        # history disagree, and the count is what the summary reports.
        monitor = AgentMonitor()
        monitor.on_message_added(_event(message=None))
        assert monitor.execution_stats["messages_added"] == 1
        assert monitor.message_history[0]["role"] == "unknown"


@pytest.mark.unit
class TestAgentMonitorMessagePreview:
    """_get_message_preview: the tool-result special case and its status override."""

    def test_an_ordinary_message_returns_its_role_and_content(self):
        preview = AgentMonitor()._get_message_preview(
            {"role": "assistant", "content": [{"text": "hello"}]}
        )
        assert preview["role"] == "assistant"
        assert preview["content"] == [{"text": "hello"}]

    def test_a_tool_result_is_relabelled_as_the_tool_role(self):
        # Strands delivers tool output as a user message; reporting it as "user"
        # would make a transcript look like the person said it.
        preview = AgentMonitor()._get_message_preview(
            {
                "role": "user",
                "content": [{"toolResult": {"status": "success", "content": []}}],
            }
        )
        assert preview["role"] == "tool"
        assert "success" in preview["content"]

    def test_a_tool_that_reports_failure_in_its_payload_overrides_a_success_status(
        self,
    ):
        # The framework marks a tool call "success" when the function returned
        # without raising. A tool that returns {"success": false} did its job and
        # reported a failure, and the transcript must say so.
        preview = AgentMonitor()._get_message_preview(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "status": "success",
                            "content": [
                                {
                                    "text": json.dumps(
                                        {"success": False, "error": "no rows"}
                                    )
                                }
                            ],
                        }
                    }
                ],
            }
        )
        assert "error" in preview["content"]

    def test_a_tool_reporting_success_in_its_payload_stays_success(self):
        preview = AgentMonitor()._get_message_preview(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "status": "success",
                            "content": [{"text": json.dumps({"success": True})}],
                        }
                    }
                ],
            }
        )
        assert "success" in preview["content"]

    def test_unparseable_tool_text_falls_back_to_the_framework_status(self):
        preview = AgentMonitor()._get_message_preview(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "status": "error",
                            "content": [{"text": "not json"}],
                        }
                    }
                ],
            }
        )
        assert "error" in preview["content"]

    def test_a_tool_result_with_no_status_reads_as_unknown(self):
        preview = AgentMonitor()._get_message_preview(
            {"role": "user", "content": [{"toolResult": {}}]}
        )
        assert "unknown" in preview["content"]

    def test_the_raw_tool_output_is_withheld_by_default(self):
        # Tool output can contain document content, and the transcript is shown in
        # the UI, so including it is opt-in.
        preview = AgentMonitor()._get_message_preview(
            {"role": "user", "content": [{"toolResult": {"status": "success"}}]}
        )
        assert "debug_tool_output" not in preview

    def test_the_raw_tool_output_is_included_when_asked_for(self):
        preview = AgentMonitor(include_debug_tool_output=True)._get_message_preview(
            {"role": "user", "content": [{"toolResult": {"status": "success"}}]}
        )
        assert preview["debug_tool_output"] == {"status": "success"}

    def test_a_user_message_that_is_not_a_tool_result_keeps_the_user_role(self):
        preview = AgentMonitor()._get_message_preview(
            {"role": "user", "content": [{"text": "a question"}]}
        )
        assert preview["role"] == "user"

    @pytest.mark.parametrize("message", [None, {}, {"role": "user"}, "a string"])
    def test_a_message_missing_its_fields_reports_the_extraction_failure(self, message):
        preview = AgentMonitor()._get_message_preview(message)
        assert preview["role"] == "unknown"
        assert "Error extracting message" in preview["content"]


@pytest.mark.unit
class TestAgentMonitorToolResultPreview:
    """_get_tool_result_preview: the one-line summary written to the log."""

    def test_a_strands_result_reporting_success_names_the_csv_uri(self):
        preview = AgentMonitor()._get_tool_result_preview(
            {
                "content": [
                    {
                        "text": json.dumps(
                            {"success": True, "result_csv_s3_uri": "s3://b/k.csv"}
                        )
                    }
                ]
            }
        )
        assert preview == "Success: s3://b/k.csv"

    def test_a_strands_result_with_data_but_no_uri_falls_back_to_data(self):
        preview = AgentMonitor()._get_tool_result_preview(
            {"content": [{"text": json.dumps({"success": True, "data": [1, 2]})}]}
        )
        assert preview.startswith("Success:")
        assert "1" in preview

    def test_a_strands_result_reporting_failure_names_the_error(self):
        preview = AgentMonitor()._get_tool_result_preview(
            {"content": [{"text": json.dumps({"success": False, "error": "no rows"})}]}
        )
        assert preview == "Error: no rows"

    def test_a_failure_with_no_error_text_still_reads_as_an_error(self):
        preview = AgentMonitor()._get_tool_result_preview(
            {"content": [{"text": json.dumps({"success": False})}]}
        )
        assert preview == "Error: Unknown error"

    def test_a_plain_dict_with_a_success_flag_is_summarised(self):
        assert (
            AgentMonitor()._get_tool_result_preview({"success": True, "data": "ok"})
            == "Success: ok"
        )

    def test_a_dict_without_a_success_flag_is_stringified(self):
        assert AgentMonitor()._get_tool_result_preview({"rows": 3}) == "{'rows': 3}"

    def test_a_non_dict_result_is_stringified(self):
        assert AgentMonitor()._get_tool_result_preview("plain text") == "plain text"

    def test_unparseable_content_text_falls_through_to_generic_handling(self):
        preview = AgentMonitor()._get_tool_result_preview(
            {"content": [{"text": "not json"}]}
        )
        assert "not json" in preview


@pytest.mark.unit
class TestAgentMonitorModelExceptions:
    """_handle_model_exception: throttle classification and the callback."""

    def _monitor(self):
        callback = MagicMock()
        return AgentMonitor(throttling_callback=callback), callback

    @pytest.mark.parametrize("code", THROTTLING_CODES)
    def test_a_throttling_client_error_invokes_the_callback(self, code):
        monitor, callback = self._monitor()
        error = _client_error(code)
        monitor._handle_model_exception(error)
        callback.assert_called_once_with(error)

    @pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException"])
    def test_a_non_throttling_client_error_does_not_invoke_the_callback(self, code):
        # The callback tells a user to wait. Firing it for a malformed request
        # would promise that waiting helps.
        monitor, callback = self._monitor()
        monitor._handle_model_exception(_client_error(code))
        callback.assert_not_called()

    def test_a_non_client_error_naming_a_throttle_invokes_the_callback(self):
        # Strands raises ModelThrottledException as a plain exception, not a
        # ClientError, so the substring route is the only one that sees it.
        monitor, callback = self._monitor()
        error = type("ModelThrottledException", (Exception,), {})("too many requests")
        monitor._handle_model_exception(error)
        callback.assert_called_once_with(error)

    @pytest.mark.parametrize(
        "text", ["throttled by bedrock", "THROTTLING detected", "Too many requests"]
    )
    def test_the_substring_route_is_case_insensitive(self, text):
        monitor, callback = self._monitor()
        monitor._handle_model_exception(RuntimeError(text))
        callback.assert_called_once()

    def test_a_non_throttling_code_whose_message_mentions_throttling_is_not_retried(
        self,
    ):
        # The two routes inside AgentMonitor disagree on exactly this input: a
        # ClientError whose error CODE is not a throttle but whose MESSAGE mentions
        # one. The code is authoritative, so the callback must not fire — otherwise a
        # ServiceUnavailableException whose cause text says "throttling" would tell
        # the user to wait for a rate limit that is not the problem.
        monitor, callback = self._monitor()
        monitor._handle_model_exception(
            _client_error(
                "ServiceUnavailableException",
                message="upstream reported throttling of a dependency",
            )
        )
        callback.assert_not_called()

    def test_an_unrelated_exception_does_not_invoke_the_callback(self):
        monitor, callback = self._monitor()
        monitor._handle_model_exception(RuntimeError("connection reset"))
        callback.assert_not_called()

    def test_no_callback_configured_is_not_an_error(self):
        AgentMonitor()._handle_model_exception(_client_error("ThrottlingException"))

    def test_a_callback_that_raises_does_not_propagate(self):
        # The callback is user-supplied and runs inside a model-invocation hook; a
        # failure there must not abort the agent's own request.
        monitor = AgentMonitor(
            throttling_callback=MagicMock(side_effect=RuntimeError())
        )
        monitor._handle_model_exception(_client_error("ThrottlingException"))

    def test_a_callback_that_raises_on_the_substring_route_also_does_not_propagate(
        self,
    ):
        monitor = AgentMonitor(
            throttling_callback=MagicMock(side_effect=RuntimeError())
        )
        monitor._handle_model_exception(RuntimeError("throttled"))


@pytest.mark.unit
class TestAgentMonitorReporting:
    """get_execution_report, reset_stats and the summary."""

    def _populated(self):
        monitor = AgentMonitor()
        monitor.on_before_invocation(_event(agent="a"))
        monitor.on_message_added(_event(message={"role": "user", "content": []}))
        monitor.tool_history.append({"tool_name": "athena"})
        monitor.model_history.append({"timestamp": "t"})
        return monitor

    def test_the_report_carries_all_four_sections(self):
        report = self._populated().get_execution_report()
        assert set(report) == {
            "execution_stats",
            "message_history",
            "tool_history",
            "model_history",
        }
        assert report["execution_stats"]["messages_added"] == 1

    def test_the_report_is_a_copy_so_a_caller_cannot_mutate_the_monitor(self):
        monitor = self._populated()
        report = monitor.get_execution_report()
        report["message_history"].clear()
        report["execution_stats"]["messages_added"] = 99
        assert len(monitor.message_history) == 1
        assert monitor.execution_stats["messages_added"] == 1

    def test_reset_clears_the_counters_and_every_history(self):
        monitor = self._populated()
        monitor.reset_stats()
        assert monitor.execution_stats["messages_added"] == 0
        assert monitor.execution_stats["requests_processed"] == 0
        assert monitor.execution_stats["start_time"] is None
        assert monitor.message_history == []
        assert monitor.tool_history == []
        assert monitor.model_history == []

    def test_the_summary_runs_with_a_complete_window(self):
        monitor = self._populated()
        monitor.on_after_invocation(_event(agent="a"))
        monitor.log_execution_summary()

    def test_the_summary_runs_with_no_window_at_all(self):
        AgentMonitor().log_execution_summary()


@pytest.mark.unit
class TestThrottlingMonitor:
    """ThrottlingMonitor: error-code classification and the DynamoDB write."""

    def _monitor(self, db_logger=None):
        return ThrottlingMonitor(job_id="job-1", user_id="user-1", db_logger=db_logger)

    @pytest.mark.parametrize("code", THROTTLING_CODES)
    def test_a_throttling_code_is_recorded_with_its_job_and_user(self, code):
        monitor = self._monitor()
        monitor._handle_model_exception(_client_error(code, "rate exceeded"))
        events = monitor.get_throttling_events()
        assert len(events) == 1
        assert events[0]["job_id"] == "job-1"
        assert events[0]["user_id"] == "user-1"
        assert events[0]["error_code"] == code
        assert events[0]["error_message"] == "rate exceeded"
        assert events[0]["timestamp"]

    @pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException"])
    def test_a_non_throttling_code_is_not_recorded(self, code):
        monitor = self._monitor()
        monitor._handle_model_exception(_client_error(code))
        assert monitor.get_throttling_events() == []

    def test_a_non_client_error_is_not_recorded_even_when_it_names_a_throttle(self):
        # This is where ThrottlingMonitor and AgentMonitor disagree: AgentMonitor
        # has a substring route for exactly this exception and this class does not,
        # so a Strands ModelThrottledException reaches the user's callback but never
        # reaches the DynamoDB record. Both providers are registered on the same
        # agent, so the asymmetry is worth pinning rather than discovering.
        monitor = self._monitor()
        monitor._handle_model_exception(
            type("ModelThrottledException", (Exception,), {})("too many requests")
        )
        assert monitor.get_throttling_events() == []

    def test_the_event_is_written_to_dynamodb_when_a_logger_is_configured(self):
        db_logger = MagicMock()
        monitor = self._monitor(db_logger)
        monitor._handle_model_exception(_client_error("ThrottlingException"))
        db_logger.log_throttling_event_async.assert_called_once()
        args = db_logger.log_throttling_event_async.call_args.args
        assert args[0] == "job-1"
        assert args[1] == "user-1"

    def test_a_dynamodb_failure_does_not_lose_the_local_record(self):
        # The local list is what get_throttling_events returns, and it is the only
        # copy if the write fails.
        db_logger = MagicMock()
        db_logger.log_throttling_event_async.side_effect = RuntimeError("throttled too")
        monitor = self._monitor(db_logger)
        monitor._handle_model_exception(_client_error("ThrottlingException"))
        assert len(monitor.get_throttling_events()) == 1

    def test_no_db_logger_is_not_an_error(self):
        monitor = self._monitor()
        monitor._handle_model_exception(_client_error("ThrottlingException"))
        assert len(monitor.get_throttling_events()) == 1

    def test_the_returned_events_are_a_copy(self):
        monitor = self._monitor()
        monitor._handle_model_exception(_client_error("ThrottlingException"))
        monitor.get_throttling_events().clear()
        assert len(monitor.get_throttling_events()) == 1

    def test_clearing_removes_the_recorded_events(self):
        monitor = self._monitor()
        monitor._handle_model_exception(_client_error("ThrottlingException"))
        monitor.clear_throttling_events()
        assert monitor.get_throttling_events() == []

    def test_several_throttles_accumulate(self):
        monitor = self._monitor()
        for _ in range(3):
            monitor._handle_model_exception(_client_error("ThrottlingException"))
        assert len(monitor.get_throttling_events()) == 3


@pytest.mark.unit
class TestMessageTrackerEvents:
    """MessageTracker: the lightweight alternative to AgentMonitor."""

    def test_a_message_is_recorded_with_a_timestamp(self):
        tracker = MessageTracker()
        tracker.on_message_added(_event(message={"role": "user"}))
        messages = tracker.get_messages()
        assert len(messages) == 1
        assert messages[0]["timestamp"]

    def test_the_raw_message_is_kept_unaltered(self):
        # Unlike AgentMonitor this tracker does no preview extraction, so the
        # caller gets exactly what the agent produced.
        message = {"role": "assistant", "content": [{"text": "hi"}]}
        tracker = MessageTracker()
        tracker.on_message_added(_event(message=message))
        assert tracker.get_messages()[0]["message"] is message

    def test_a_dict_message_has_no_role_attribute_and_reads_as_unknown(self):
        # The role is read with getattr, not subscripting, so a plain dict message
        # -- which is what Strands delivers -- records "unknown".
        tracker = MessageTracker()
        tracker.on_message_added(_event(message={"role": "user"}))
        assert tracker.get_messages()[0]["role"] == "unknown"

    def test_an_object_message_with_a_role_attribute_records_it(self):
        tracker = MessageTracker()
        tracker.on_message_added(_event(message=_event(role="assistant")))
        assert tracker.get_messages()[0]["role"] == "assistant"

    def test_the_callback_receives_the_event_and_the_recorded_data(self):
        callback = MagicMock()
        tracker = MessageTracker(callback_fn=callback)
        event = _event(message={"role": "user"})
        tracker.on_message_added(event)
        called_event, data = callback.call_args.args
        assert called_event is event
        assert data["message"] == {"role": "user"}

    def test_a_callback_that_raises_does_not_lose_the_message(self):
        tracker = MessageTracker(callback_fn=MagicMock(side_effect=RuntimeError()))
        tracker.on_message_added(_event(message={"role": "user"}))
        assert len(tracker.get_messages()) == 1

    def test_the_returned_messages_are_a_copy(self):
        tracker = MessageTracker()
        tracker.on_message_added(_event(message={"role": "user"}))
        tracker.get_messages().clear()
        assert len(tracker.get_messages()) == 1

    def test_clearing_removes_the_recorded_messages(self):
        tracker = MessageTracker()
        tracker.on_message_added(_event(message={"role": "user"}))
        tracker.clear_messages()
        assert tracker.get_messages() == []


@pytest.mark.unit
class TestHookRegistration:
    """register_hooks: the four core events are always registered."""

    def test_agent_monitor_registers_the_four_core_callbacks(self):
        registry = MagicMock()
        AgentMonitor().register_hooks(registry)
        assert registry.add_callback.call_count >= 4

    def test_message_tracker_registers_exactly_one_callback(self):
        registry = MagicMock()
        MessageTracker().register_hooks(registry)
        registry.add_callback.assert_called_once()

    def test_throttling_monitor_registration_does_not_raise(self):
        # Whether it registers anything depends on whether the installed strands
        # exposes the experimental model-invocation events, so the assertion is
        # only that registration is safe either way.
        ThrottlingMonitor(job_id="j", user_id="u").register_hooks(MagicMock())
