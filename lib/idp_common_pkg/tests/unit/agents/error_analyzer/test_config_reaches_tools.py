# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The error analyzer's window and log-group settings reach the CloudWatch call.

``agents.error_analyzer.lookback_hours`` and
``agents.error_analyzer.parameters.max_log_groups`` used to have no effect: each
tool declared its own default in its signature (``hours_back: int = 24``,
``max_log_groups: int = 20``), and Python supplies that default whenever the model
omits the argument, so the configured value was never consulted (#1555).

These tests build the real agent through ``create_error_analyzer_agent`` and invoke
the tool through the agent's own Strands tool executor with no arguments -- the
shape of a model call that omits them. The configuration carries non-default values,
and the assertions are made on the parameters of the botocore ``FilterLogEvents``
requests, captured from a real ``logs`` client under a ``Stubber``. ``FilterLogEvents``
is issued once per log group with no pagination, so a request count is a log-group
count.

``max_log_events`` is deliberately not wired: its configured default (5) differs
from the tool's (10), and every saved profile stores the 5, so wiring it would change
the number of events every existing deployment receives. That is recorded on #1555 as
a decision for the maintainers, and the tests below pin the current behaviour so the
decision cannot be reversed by accident.
"""

import json
from datetime import timedelta
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

    def test_configured_max_log_events_does_not_reach_the_call(
        self, agent, filter_calls
    ):
        _invoke(
            agent,
            "search_cloudwatch_logs",
            _config(parameters={"max_log_events": 3}),
        )
        assert _limits(filter_calls) == {10}

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

    def test_window_comes_from_the_record_not_lookback_hours(self, agent, filter_calls):
        # FAILED with a CompletionTime: a 2.5-minute buffer either side of it.
        self._search(agent, _config(lookback_hours=72))
        assert _windows_hours(filter_calls) == {5 / 60}


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

    def test_configured_max_log_events_does_not_reach_the_call(
        self, agent, filter_calls
    ):
        _invoke(
            agent,
            "search_performance_issues",
            _config(parameters={"max_log_events": 3}),
        )
        assert _limits(filter_calls) == {10}
