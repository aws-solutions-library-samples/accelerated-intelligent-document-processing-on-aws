# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The error analyzer's window, log-group and event settings reach the CloudWatch call.

``agents.error_analyzer.lookback_hours`` and
``agents.error_analyzer.parameters.max_log_groups`` and ``.max_log_events`` used to
have no effect: each tool declared its own default in its signature
(``hours_back: int = 24``, ``max_log_groups: int = 20``, ``max_log_events: int =
10``), and Python supplies that default whenever the model omits the argument, so
the configured value was never consulted (#1555).

These tests build the real agent through ``create_error_analyzer_agent`` and invoke
the tool through the agent's own Strands tool executor with no arguments -- the
shape of a model call that omits them. The configuration carries non-default values,
and the assertions are made on the parameters of the botocore ``FilterLogEvents``
requests, captured from a real ``logs`` client under a ``Stubber``. ``FilterLogEvents``
is issued once per log group with no pagination, so a request count is a log-group
count.

``max_log_events`` had a configured default (5) that differed from the tool's (10),
and every saved profile stores that 5. Its default is now 10, and the v0.9 -> v0.10
configuration migration raises a stored 5 to 10, so a profile saved before the change
keeps the 10 events per log group it was getting. ``TestStoredOldDefault`` drives
that through the same loading path a saved profile takes.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import botocore.session
import pytest
import strands
from botocore.stub import ANY, Stubber

from idp_common.config.models import IDPConfig
from idp_common.monitoring import cloudwatch_logs_service

pytestmark = pytest.mark.unit

# More groups than both the shipped group cap (20) and the clamp (50), so a test can
# tell either from "every group".
LOG_GROUPS = [f"/stack/lambda/Step{i:02d}Function" for i in range(60)]
SETTINGS_PARAMETER = "/test/settings"
HOUR_MS = int(timedelta(hours=1).total_seconds() * 1000)


def _client(service: str):
    return botocore.session.get_session().create_client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def _config(**error_analyzer: Any) -> IDPConfig:
    """Build an IDPConfig whose error_analyzer section carries the given values."""
    params = error_analyzer.pop("parameters", {})
    data: Dict[str, Any] = {
        "agents": {"error_analyzer": {**error_analyzer, "parameters": params}}
    }
    return IDPConfig.model_validate(data)


@pytest.fixture
def agent():
    """The real agent, built with no AWS access."""
    if isinstance(strands, MagicMock) or not isinstance(strands.Agent, type):
        pytest.fail(
            "strands is stubbed in this environment, so the agent's tool executor "
            "cannot be driven and these tests would measure nothing."
        )
    from idp_common.agents.error_analyzer.agent import create_error_analyzer_agent

    with (
        patch(
            "idp_common.agents.error_analyzer.agent.get_config",
            return_value=IDPConfig(),
        ),
        patch(
            "idp_common.agents.error_analyzer.agent.create_strands_bedrock_model",
            return_value=MagicMock(),
        ),
        patch(
            "idp_common.agents.error_analyzer.agent.get_error_analyzer_model_id",
            return_value="test-model",
        ),
        patch("boto3.Session"),
    ):
        built = create_error_analyzer_agent()
    assert isinstance(built, strands.Agent)
    return built


@pytest.fixture
def filter_calls(monkeypatch):
    """Stub SSM (log-group list) and CloudWatch Logs; yield the captured requests."""
    monkeypatch.setenv("SETTINGS_PARAMETER_NAME", SETTINGS_PARAMETER)
    monkeypatch.setenv("AWS_STACK_NAME", "stack")

    ssm = _client("ssm")
    ssm_stub = Stubber(ssm)
    ssm_stub.add_response(
        "get_parameter",
        {
            "Parameter": {
                "Name": SETTINGS_PARAMETER,
                "Value": json.dumps({"CloudWatchLogGroups": ",".join(LOG_GROUPS)}),
            }
        },
        {"Name": SETTINGS_PARAMETER},
    )
    ssm_stub.activate()
    cloudwatch_logs_service.reset_settings_cache(ssm_client=ssm)

    logs = _client("logs")
    captured: List[Dict[str, Any]] = []
    logs.meta.events.register(
        "provide-client-params.logs.FilterLogEvents",
        lambda params, **_: captured.append(dict(params)),
    )
    logs_stub = Stubber(logs)
    # One response per log group at most; FilterLogEvents is a single call per
    # group, so there is no paginator to terminate.
    for _ in LOG_GROUPS:
        logs_stub.add_response(
            "filter_log_events",
            {"events": []},
            {
                "logGroupName": ANY,
                "startTime": ANY,
                "endTime": ANY,
                "limit": ANY,
                "filterPattern": ANY,
            },
        )
    logs_stub.activate()
    monkeypatch.setattr(
        cloudwatch_logs_service.boto3, "client", lambda service, **_: logs
    )

    yield captured

    ssm_stub.assert_no_pending_responses()
    cloudwatch_logs_service.reset_settings_cache()


def _invoke(agent, tool_name: str, config: IDPConfig, **kwargs: Any) -> Dict[str, Any]:
    with patch(
        "idp_common.agents.error_analyzer.tools.cloudwatch_tool.get_config",
        return_value=config,
    ):
        result = getattr(agent.tool, tool_name)(record_direct_tool_call=False, **kwargs)
    assert result["status"] == "success", result
    return result


def _epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _groups(calls: List[Dict[str, Any]]) -> List[str]:
    return [c["logGroupName"] for c in calls]


def _windows_hours(calls: List[Dict[str, Any]]) -> set:
    assert calls, "no FilterLogEvents request was made, so there is no window"
    return {(c["endTime"] - c["startTime"]) / HOUR_MS for c in calls}


def _limits(calls: List[Dict[str, Any]]) -> set:
    assert calls, "no FilterLogEvents request was made, so there is no limit"
    return {c["limit"] for c in calls}


class TestSearchCloudwatchLogs:
    def test_shipped_defaults_keep_the_previous_effective_values(
        self, agent, filter_calls
    ):
        # The previous signature defaults were hours_back=24, max_log_groups=20 and
        # max_log_events=10. The "ERROR" pattern does not widen the request limit,
        # so the limit is the event count itself.
        _invoke(agent, "search_cloudwatch_logs", IDPConfig())
        assert _groups(filter_calls) == LOG_GROUPS[:20]
        assert _windows_hours(filter_calls) == {24}
        assert _limits(filter_calls) == {10}

    def test_configured_lookback_reaches_the_call(self, agent, filter_calls):
        _invoke(agent, "search_cloudwatch_logs", _config(lookback_hours=72))
        assert _groups(filter_calls) == LOG_GROUPS[:20]
        assert _windows_hours(filter_calls) == {72}

    def test_configured_group_cap_reaches_the_call(self, agent, filter_calls):
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(parameters={"max_log_groups": 2}),
        )
        assert _groups(filter_calls) == LOG_GROUPS[:2]
        assert _windows_hours(filter_calls) == {24}

    def test_configured_values_above_the_advertised_maximum_are_clamped(
        self, agent, filter_calls
    ):
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(lookback_hours=1000, parameters={"max_log_groups": 500}),
        )
        assert _groups(filter_calls) == LOG_GROUPS[:50]
        assert _windows_hours(filter_calls) == {168}

    def test_a_stored_null_lookback_searches_the_default_window(
        self, agent, filter_calls
    ):
        # A saved profile carrying ``lookback_hours: null`` used to fail to load at
        # all (``int(None)``), so the tool never reached CloudWatch.
        _invoke(agent, "search_cloudwatch_logs", _config(lookback_hours=None))
        assert _windows_hours(filter_calls) == {24}

    def test_explicit_arguments_still_win(self, agent, filter_calls):
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(lookback_hours=72, parameters={"max_log_groups": 2}),
            hours_back=6,
            max_log_groups=3,
        )
        assert _groups(filter_calls) == LOG_GROUPS[:3]
        assert _windows_hours(filter_calls) == {6}

    def test_explicit_null_resolves_to_the_configured_values(self, agent, filter_calls):
        # The schema advertises ``default: null``, so a model may send null
        # explicitly; the Optional annotation is what lets Strands validate it.
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(lookback_hours=72, parameters={"max_log_groups": 2}),
            hours_back=None,
            max_log_groups=None,
        )
        assert _groups(filter_calls) == LOG_GROUPS[:2]
        assert _windows_hours(filter_calls) == {72}

    def test_explicit_values_are_not_clamped(self, agent, filter_calls):
        # The clamp applies to configured defaults only, as before for model values.
        _invoke(
            agent,
            "search_cloudwatch_logs",
            IDPConfig(),
            hours_back=200,
            max_log_groups=55,
        )
        assert _groups(filter_calls) == LOG_GROUPS[:55]
        assert _windows_hours(filter_calls) == {200}

    def test_configured_max_log_events_reaches_the_call(self, agent, filter_calls):
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(parameters={"max_log_events": 3}),
        )
        assert _limits(filter_calls) == {3}

    def test_configured_max_log_events_above_the_maximum_is_clamped(
        self, agent, filter_calls
    ):
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(parameters={"max_log_events": 500}),
        )
        assert _limits(filter_calls) == {50}

    def test_explicit_max_log_events_still_wins(self, agent, filter_calls):
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(parameters={"max_log_events": 3}),
            max_log_events=7,
        )
        assert _limits(filter_calls) == {7}

    def test_explicit_null_max_log_events_resolves_to_the_configured_count(
        self, agent, filter_calls
    ):
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(parameters={"max_log_events": 3}),
            max_log_events=None,
        )
        assert _limits(filter_calls) == {3}

    def test_explicit_max_log_events_is_not_clamped(self, agent, filter_calls):
        _invoke(agent, "search_cloudwatch_logs", IDPConfig(), max_log_events=60)
        assert _limits(filter_calls) == {60}

    def test_unreadable_section_falls_back_to_the_previous_values(
        self, agent, filter_calls
    ):
        # ``agents.error_analyzer`` is Optional; when it is absent the tool uses its
        # own fallback values, which must be the same 24 hours and 20 groups.
        config = IDPConfig()
        config.agents.error_analyzer = None
        _invoke(agent, "search_cloudwatch_logs", config)
        assert _groups(filter_calls) == LOG_GROUPS[:20]
        assert _windows_hours(filter_calls) == {24}
        assert _limits(filter_calls) == {10}


class TestDocumentSearch:
    """The document branch takes the group cap; its window comes from the record."""

    RECORD = {
        "document_found": True,
        "document": {
            "ObjectKey": "doc.pdf",
            "ObjectStatus": "FAILED",
            "InitialEventTime": "2026-01-01T00:00:00Z",
            "CompletionTime": "2026-01-01T00:10:00Z",
        },
    }

    def _search(self, agent, config: IDPConfig, **kwargs: Any) -> None:
        with patch(
            "idp_common.agents.error_analyzer.tools.cloudwatch_tool"
            ".fetch_document_record",
            return_value=self.RECORD,
        ):
            _invoke(
                agent, "search_cloudwatch_logs", config, document_id="doc.pdf", **kwargs
            )

    def test_shipped_default_searches_twenty_groups(self, agent, filter_calls):
        self._search(agent, IDPConfig())
        assert _groups(filter_calls) == LOG_GROUPS[:20]

    def test_configured_group_cap_reaches_the_call(self, agent, filter_calls):
        self._search(agent, _config(parameters={"max_log_groups": 2}))
        assert _groups(filter_calls) == LOG_GROUPS[:2]

    def test_explicit_group_cap_still_wins(self, agent, filter_calls):
        self._search(agent, _config(parameters={"max_log_groups": 2}), max_log_groups=4)
        assert _groups(filter_calls) == LOG_GROUPS[:4]

    def test_configured_event_count_reaches_the_call(self, agent, filter_calls):
        self._search(agent, _config(parameters={"max_log_events": 3}))
        assert _limits(filter_calls) == {3}

    def test_shipped_default_event_count_is_ten(self, agent, filter_calls):
        self._search(agent, IDPConfig())
        assert _limits(filter_calls) == {10}

    @pytest.mark.parametrize("requested", [0, -1])
    def test_a_count_below_one_uses_the_configured_count(
        self, agent, filter_calls, requested
    ):
        # The document branch has its own route to search_log_group; without the
        # resolution there, the service's floor would quietly turn 0 into 1.
        self._search(
            agent, _config(parameters={"max_log_events": 3}), max_log_events=requested
        )
        assert _limits(filter_calls) == {3}

    def test_window_comes_from_the_record_not_lookback_hours(self, agent, filter_calls):
        # FAILED with a CompletionTime: a 2.5-minute buffer either side of it.
        self._search(agent, _config(lookback_hours=72))
        assert _windows_hours(filter_calls) == {5 / 60}

    def _window_for(self, agent, filter_calls, document: Dict[str, Any]) -> set:
        record = {"document_found": True, "document": document}
        with patch(
            "idp_common.agents.error_analyzer.tools.cloudwatch_tool"
            ".fetch_document_record",
            return_value=record,
        ):
            _invoke(agent, "search_cloudwatch_logs", IDPConfig(), document_id="d.pdf")
        assert filter_calls, "no FilterLogEvents request was made"
        return {(c["startTime"], c["endTime"]) for c in filter_calls}

    def test_a_finished_run_is_searched_from_start_to_completion(
        self, agent, filter_calls
    ):
        window = self._window_for(
            agent,
            filter_calls,
            {
                "ObjectKey": "d.pdf",
                "ObjectStatus": "COMPLETED",
                "InitialEventTime": "2026-01-01T00:00:00Z",
                "CompletionTime": "2026-01-01T00:10:00Z",
            },
        )
        start = _epoch_ms("2026-01-01T00:00:00Z") - 30_000
        end = _epoch_ms("2026-01-01T00:10:00Z") + 30_000
        assert window == {(start, end)}

    def test_a_run_in_progress_is_searched_over_the_last_half_hour(
        self, agent, filter_calls
    ):
        before = int(datetime.now().timestamp() * 1000)
        window = self._window_for(
            agent,
            filter_calls,
            {
                "ObjectKey": "d.pdf",
                "ObjectStatus": "IN_PROGRESS",
                "InitialEventTime": "2026-01-01T00:00:00Z",
            },
        )
        after = int(datetime.now().timestamp() * 1000)
        assert len(window) == 1, window
        ((start, end),) = window
        assert before <= end <= after
        assert end - start == 30 * 60_000

    def test_a_run_in_progress_with_an_old_completion_time_is_searched_now(
        self, agent, filter_calls
    ):
        # A reprocessed document is IN_PROGRESS while its record still carries the
        # previous run's CompletionTime; the status, not the missing end, has to
        # select the last half hour.
        before = int(datetime.now().timestamp() * 1000)
        window = self._window_for(
            agent,
            filter_calls,
            {
                "ObjectKey": "d.pdf",
                "ObjectStatus": "IN_PROGRESS",
                "InitialEventTime": "2026-01-01T00:00:00Z",
                "CompletionTime": "2026-01-01T00:10:00Z",
            },
        )
        after = int(datetime.now().timestamp() * 1000)
        ((start, end),) = window
        assert before <= end <= after
        assert end - start == 30 * 60_000

    def test_a_finished_run_with_no_start_ends_at_its_completion(
        self, agent, filter_calls
    ):
        # With no InitialEventTime the window used to carry no start, and
        # search_log_group then ignored the completion time as well and searched the
        # 24 hours before now -- days away from the run for an older document.
        window = self._window_for(
            agent,
            filter_calls,
            {
                "ObjectKey": "d.pdf",
                "ObjectStatus": "COMPLETED",
                "CompletionTime": "2026-01-01T00:10:00Z",
            },
        )
        completion = _epoch_ms("2026-01-01T00:10:00Z")
        assert window == {(completion - 30 * 60_000, completion + 30_000)}


class TestSearchPerformanceIssues:
    def test_shipped_default_keeps_the_previous_window(self, agent, filter_calls):
        _invoke(agent, "search_performance_issues", IDPConfig())
        assert len(filter_calls) == len(LOG_GROUPS)
        assert _windows_hours(filter_calls) == {24}
        assert _limits(filter_calls) == {10}

    def test_configured_lookback_reaches_the_call(self, agent, filter_calls):
        _invoke(agent, "search_performance_issues", _config(lookback_hours=48))
        assert _windows_hours(filter_calls) == {48}

    def test_configured_lookback_above_the_maximum_is_clamped(
        self, agent, filter_calls
    ):
        _invoke(agent, "search_performance_issues", _config(lookback_hours=1000))
        assert _windows_hours(filter_calls) == {168}

    def test_explicit_hours_back_still_wins(self, agent, filter_calls):
        _invoke(
            agent,
            "search_performance_issues",
            _config(lookback_hours=48),
            hours_back=2,
        )
        assert _windows_hours(filter_calls) == {2}

    def test_explicit_null_resolves_to_the_configured_window(self, agent, filter_calls):
        _invoke(
            agent,
            "search_performance_issues",
            _config(lookback_hours=48),
            hours_back=None,
        )
        assert _windows_hours(filter_calls) == {48}

    def test_explicit_hours_back_is_not_clamped(self, agent, filter_calls):
        _invoke(agent, "search_performance_issues", IDPConfig(), hours_back=200)
        assert _windows_hours(filter_calls) == {200}

    def test_configured_max_log_events_reaches_the_call(self, agent, filter_calls):
        _invoke(
            agent,
            "search_performance_issues",
            _config(parameters={"max_log_events": 3}),
        )
        assert _limits(filter_calls) == {3}

    def test_configured_max_log_events_above_the_maximum_is_clamped(
        self, agent, filter_calls
    ):
        _invoke(
            agent,
            "search_performance_issues",
            _config(parameters={"max_log_events": 500}),
        )
        assert _limits(filter_calls) == {50}

    def test_explicit_max_log_events_still_wins(self, agent, filter_calls):
        _invoke(
            agent,
            "search_performance_issues",
            _config(parameters={"max_log_events": 3}),
            max_log_events=7,
        )
        assert _limits(filter_calls) == {7}


class TestStoredOldDefault:
    """A profile saved before v0.10 stores ``max_log_events: 5`` and keeps 10 events.

    The configuration is loaded the way a stored profile head is: a raw dict, with
    its numbers held as strings as ``ConfigurationRecord`` writes them, validated
    through ``IDPConfig`` (whose validator runs the migration chain). The assertion
    is on the ``limit`` of the CloudWatch request, not on the loaded model, so the
    test measures the number of events a search asks for.
    """

    @staticmethod
    def _stored(stamp: Any, max_log_events: Any) -> IDPConfig:
        data: Dict[str, Any] = {
            "agents": {
                "error_analyzer": {"parameters": {"max_log_events": max_log_events}}
            }
        }
        if stamp is not None:
            data["config_format_version"] = stamp
        return IDPConfig.model_validate(data)

    @pytest.mark.parametrize("stamp", ["0.9", "0.6", None])
    @pytest.mark.parametrize("stored", ["5", 5])
    @pytest.mark.parametrize(
        "tool", ["search_cloudwatch_logs", "search_performance_issues"]
    )
    def test_a_pre_v010_stored_five_searches_ten_events(
        self, agent, filter_calls, stamp, stored, tool
    ):
        _invoke(agent, tool, self._stored(stamp, stored))
        assert _limits(filter_calls) == {10}

    def test_a_five_saved_at_v010_is_kept(self, agent, filter_calls):
        # A configuration saved by this release is stamped v0.10, so a 5 in it is
        # a choice made after the setting took effect.
        _invoke(agent, "search_cloudwatch_logs", self._stored("0.10", "5"))
        assert _limits(filter_calls) == {5}

    def test_a_pre_v010_stored_value_other_than_five_takes_effect(
        self, agent, filter_calls
    ):
        _invoke(agent, "search_cloudwatch_logs", self._stored("0.9", "3"))
        assert _limits(filter_calls) == {3}


def _events_limit_range() -> tuple:
    """The ``FilterLogEvents`` ``limit`` range, read from the botocore service model."""
    shape = (
        botocore.session.get_session()
        .get_service_model("logs")
        .operation_model("FilterLogEvents")
        .input_shape.members["limit"]
    )
    assert "min" in shape.metadata and "max" in shape.metadata, shape.metadata
    return shape.metadata["min"], shape.metadata["max"]


class TestEventLimitStaysInRange:
    """Every ``FilterLogEvents`` request carries a ``limit`` in the declared range.

    Below the service model's minimum, botocore rejects the request before it is
    sent, and ``search_log_group`` reports the rejection as a log group with no
    events, so the tool says "nothing found" rather than failing. botocore does not
    check the maximum, so a value above it would be sent to the service. The
    captured parameters are the ones handed to the client, before botocore's
    validation, so an out-of-range limit is visible here either way.
    """

    def test_the_service_bounds_match_the_service_model(self):
        assert _events_limit_range() == (
            cloudwatch_logs_service.FILTER_LOG_EVENTS_MIN_LIMIT,
            cloudwatch_logs_service.FILTER_LOG_EVENTS_MAX_LIMIT,
        )

    @pytest.mark.parametrize("requested", [0, -1])
    def test_a_performance_search_below_one_uses_the_configured_count(
        self, agent, filter_calls, requested
    ):
        _invoke(
            agent,
            "search_performance_issues",
            _config(parameters={"max_log_events": 3}),
            max_log_events=requested,
        )
        assert _limits(filter_calls) == {3}

    @pytest.mark.parametrize("requested", [0, -1])
    def test_a_log_search_below_one_uses_the_configured_count(
        self, agent, filter_calls, requested
    ):
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(parameters={"max_log_events": 3}),
            max_log_events=requested,
        )
        assert _limits(filter_calls) == {3}

    def test_a_count_of_one_is_used_as_given(self, agent, filter_calls):
        _invoke(agent, "search_performance_issues", IDPConfig(), max_log_events=1)
        assert _limits(filter_calls) == {1}

    def test_a_widened_limit_above_the_service_maximum_is_capped(
        self, agent, filter_calls
    ):
        # "Exception" widens the request limit to five times the event count, so a
        # count the model may pass (the tool's maximum is advisory) can exceed what
        # the service accepts.
        low, high = _events_limit_range()
        _invoke(
            agent,
            "search_cloudwatch_logs",
            IDPConfig(),
            filter_pattern="Exception",
            max_log_events=high // 5 + 1,
        )
        assert _limits(filter_calls) == {high}

    def test_every_request_stays_in_range_whatever_the_count(self, monkeypatch):
        low, high = _events_limit_range()
        counts = (-5, 0, 1, high, high + 1)
        logs = _client("logs")
        captured: List[Dict[str, Any]] = []
        logs.meta.events.register(
            "provide-client-params.logs.FilterLogEvents",
            lambda params, **_: captured.append(dict(params)),
        )
        stub = Stubber(logs)
        for _ in counts:
            stub.add_response(
                "filter_log_events",
                {"events": []},
                {
                    "logGroupName": ANY,
                    "startTime": ANY,
                    "endTime": ANY,
                    "limit": ANY,
                    "filterPattern": ANY,
                },
            )
        stub.activate()
        monkeypatch.setattr(
            cloudwatch_logs_service.boto3, "client", lambda service, **_: logs
        )
        for max_events in counts:
            result = cloudwatch_logs_service.search_log_group(
                LOG_GROUPS[0], filter_pattern="Exception", max_events=max_events
            )
            assert "error" not in result, (max_events, result)
        stub.assert_no_pending_responses()
        assert [c["limit"] for c in captured] == [low, low, 5, high, high]
