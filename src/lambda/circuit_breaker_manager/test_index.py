"""Unit tests for circuit_breaker_manager Lambda."""

import importlib.util
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "circuit_breaker_manager_index_under_test"


@pytest.fixture
def index_module():
    """Import index with mocked boto3 clients/resources."""
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "ALERTS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
        "RECOVERY_TIMEOUT_SECONDS": "300",
        "ERROR_HANDLER_ARN": "",
        "METRIC_NAMESPACE": "TEST_NS",
    }
    with patch.dict(os.environ, env_vars, clear=False), \
         patch("boto3.resource") as mock_resource, \
         patch("boto3.client") as mock_client:
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_client.side_effect = lambda name: MagicMock(name=f"client-{name}")

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)

        module.concurrency_table = mock_table
        module.sns = MagicMock()
        module.cloudwatch = MagicMock()
        module.lambda_client = MagicMock()
        yield module
        sys.modules.pop(_MODULE_NAME, None)


@pytest.fixture
def ddb_module(index_module, monkeypatch):
    """``index_module`` with a REAL DynamoDB table behind ``concurrency_table``.

    The HALF_OPEN -> OPEN transition is guarded by a ``ConditionExpression``,
    and a ``MagicMock`` table "applies" every conditional write, so a mock
    cannot show that two concurrent health checks make one transition rather
    than two. ``moto`` evaluates the expression for real and raises a real
    ``ConditionalCheckFailedException``. It is also what makes the OPEN ->
    HALF_OPEN -> OPEN cycle assertable end to end, since each step reads what
    the previous one actually persisted.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        # A Session, not the module-level ``boto3.resource``: the
        # ``index_module`` fixture has that one patched for the duration, so
        # calling it here would hand back the MagicMock whose conditional
        # writes all "succeed" — the very thing this fixture replaces.
        session = boto3.session.Session(region_name="us-east-1")
        table = session.resource("dynamodb").create_table(
            TableName="test-concurrency",
            KeySchema=[{"AttributeName": "counter_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "counter_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        index_module.concurrency_table = table
        yield index_module


def _put_breaker(module, **attrs) -> None:
    module.concurrency_table.put_item(
        Item={"counter_id": module.CIRCUIT_BREAKER_ID, **attrs}
    )


def _breaker_item(module) -> dict:
    response = module.concurrency_table.get_item(
        Key={"counter_id": module.CIRCUIT_BREAKER_ID}
    )
    return response.get("Item") or {}


def _ago(seconds: float) -> str:
    """An ISO8601 timestamp ``seconds`` in the past, as the manager writes it."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _seconds_since(timestamp: str) -> float:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _cond_check_failed() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
        "UpdateItem",
    )


class TestHandleAlarmEvent:
    """Covers handle_alarm_event for ALARM/OK transitions."""

    def test_alarm_from_closed_opens_breaker(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "CLOSED", "failure_count": 0}
        }
        index_module.handle_alarm_event("ALARM", "Bedrock 5xx")
        index_module.concurrency_table.update_item.assert_called_once()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "OPEN"
        assert kwargs["ExpressionAttributeValues"][":expected"] == "CLOSED"
        index_module.sns.publish.assert_called_once()

    def test_alarm_from_half_open_reopens(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "HALF_OPEN", "failure_count": 1}
        }
        index_module.handle_alarm_event("ALARM", "Bedrock 5xx again")
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "OPEN"
        assert kwargs["ExpressionAttributeValues"][":expected"] == "HALF_OPEN"
        assert kwargs["ExpressionAttributeValues"][":fc"] == 2

    def test_alarm_from_open_is_noop(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN"}
        }
        index_module.handle_alarm_event("ALARM", "still down")
        index_module.concurrency_table.update_item.assert_not_called()
        index_module.sns.publish.assert_not_called()

    def test_ok_from_open_transitions_half_open(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN"}
        }
        index_module.handle_alarm_event("OK", "alarm cleared")
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "HALF_OPEN"
        assert kwargs["ExpressionAttributeValues"][":expected"] == "OPEN"
        index_module.sns.publish.assert_called_once()

    def test_ok_from_closed_is_noop(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "CLOSED"}
        }
        index_module.handle_alarm_event("OK", "alarm cleared")
        index_module.concurrency_table.update_item.assert_not_called()

    def test_alarm_conditional_failure_skips_side_effects(self, index_module):
        """If a concurrent writer already changed state, side effects must be skipped."""
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "CLOSED", "failure_count": 0}
        }
        index_module.concurrency_table.update_item.side_effect = _cond_check_failed()
        index_module.handle_alarm_event("ALARM", "Bedrock 5xx")
        index_module.sns.publish.assert_not_called()
        index_module.lambda_client.invoke.assert_not_called()

    def test_ok_preserves_failure_count(self, index_module):
        """OPEN->HALF_OPEN on alarm OK must not reset failure_count."""
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN", "failure_count": 7}
        }
        index_module.handle_alarm_event("OK", "alarm cleared")
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert ":fc" not in kwargs["ExpressionAttributeValues"]
        assert "failure_count" not in kwargs["UpdateExpression"]


class TestHandleHealthCheck:
    """Covers handle_health_check recovery timeout elapsed vs not."""

    def test_recovery_timeout_elapsed_transitions_open_to_half_open(self, index_module):
        opened_at = (
            datetime.now(timezone.utc) - timedelta(seconds=400)
        ).isoformat()
        index_module.concurrency_table.get_item.return_value = {
            "Item": {
                "state": "OPEN",
                "opened_at": opened_at,
                "recovery_attempts": 2,
            }
        }
        index_module.handle_health_check()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "HALF_OPEN"
        assert kwargs["ExpressionAttributeValues"][":expected"] == "OPEN"
        assert kwargs["ExpressionAttributeValues"][":ra"] == 3

    def test_health_check_preserves_failure_count(self, index_module):
        """Health-check OPEN->HALF_OPEN must not reset failure_count."""
        opened_at = (
            datetime.now(timezone.utc) - timedelta(seconds=400)
        ).isoformat()
        index_module.concurrency_table.get_item.return_value = {
            "Item": {
                "state": "OPEN",
                "opened_at": opened_at,
                "failure_count": 9,
                "recovery_attempts": 2,
            }
        }
        index_module.handle_health_check()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert ":fc" not in kwargs["ExpressionAttributeValues"]
        assert "failure_count" not in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeValues"][":ra"] == 3

    def test_recovery_timeout_not_elapsed_stays_open(self, index_module):
        opened_at = (
            datetime.now(timezone.utc) - timedelta(seconds=30)
        ).isoformat()
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN", "opened_at": opened_at}
        }
        index_module.handle_health_check()
        index_module.concurrency_table.update_item.assert_not_called()

    def test_health_check_closed_is_noop(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "CLOSED"}
        }
        index_module.handle_health_check()
        index_module.concurrency_table.update_item.assert_not_called()


class TestHandlerActions:
    """Covers manual reset and get_state actions via handler()."""

    def test_get_state_action(self, index_module):
        state_item = {
            "counter_id": "circuit_breaker",
            "state": "OPEN",
            "failure_count": 1,
        }
        index_module.concurrency_table.get_item.return_value = {"Item": state_item}
        result = index_module.handler({"action": "get_state"}, MagicMock())
        assert result["statusCode"] == 200
        assert result["body"] == state_item

    def test_reset_action_forces_closed(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN"}
        }
        result = index_module.handler({"action": "reset"}, MagicMock())
        assert result["statusCode"] == 200
        index_module.concurrency_table.update_item.assert_called_once()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "CLOSED"
        # Manual reset zeros counters and removes stale last_error + opened_at.
        assert kwargs["ExpressionAttributeValues"][":fc"] == 0
        assert kwargs["ExpressionAttributeValues"][":ra"] == 0
        assert "REMOVE" in kwargs["UpdateExpression"]
        assert "last_error" in kwargs["UpdateExpression"]
        assert "opened_at" in kwargs["UpdateExpression"]
        index_module.sns.publish.assert_called_once()


class TestManualActions:
    """Covers manual_open / manual_close / manual_probe admin actions."""

    def test_manual_open_from_closed(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "CLOSED", "failure_count": 3}
        }
        event = {"action": "manual_open", "user": "admin@x", "reason": "quiesce"}
        result = index_module.handler(event, MagicMock())
        assert result["statusCode"] == 200
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "OPEN"
        assert kwargs["ExpressionAttributeValues"][":expected"] == "CLOSED"
        # Admin pauses are operator actions, not Bedrock failures, so
        # failure_count must be left alone.
        assert ":fc" not in kwargs["ExpressionAttributeValues"]
        assert "failure_count" not in kwargs["UpdateExpression"]
        assert "Manual pause by admin@x: quiesce" in (
            kwargs["ExpressionAttributeValues"][":err"]
        )
        index_module.sns.publish.assert_called_once()

    def test_manual_open_from_open_is_noop(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN"}
        }
        event = {"action": "manual_open", "user": "admin@x", "reason": "x"}
        index_module.handler(event, MagicMock())
        index_module.concurrency_table.update_item.assert_not_called()
        index_module.sns.publish.assert_not_called()

    def test_manual_open_conditional_failure_skips_side_effects(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "CLOSED"}
        }
        index_module.concurrency_table.update_item.side_effect = _cond_check_failed()
        index_module.handler(
            {"action": "manual_open", "user": "a", "reason": "r"}, MagicMock()
        )
        index_module.sns.publish.assert_not_called()

    def test_manual_close_unconditional_from_open(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN", "failure_count": 5, "recovery_attempts": 2}
        }
        event = {"action": "manual_close", "user": "admin@x", "reason": "override"}
        result = index_module.handler(event, MagicMock())
        assert result["statusCode"] == 200
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "CLOSED"
        assert kwargs["ExpressionAttributeValues"][":fc"] == 0
        assert kwargs["ExpressionAttributeValues"][":ra"] == 0
        assert "REMOVE" in kwargs["UpdateExpression"]
        assert "last_error" in kwargs["UpdateExpression"]
        # opened_at must also be cleared so the UI panel doesn't show a stale
        # outage timestamp after a manual override.
        assert "opened_at" in kwargs["UpdateExpression"]
        assert "ConditionExpression" not in kwargs
        index_module.sns.publish.assert_called_once()

    def test_manual_probe_from_open(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN", "recovery_attempts": 1}
        }
        event = {"action": "manual_probe", "user": "admin@x", "reason": "test"}
        index_module.handler(event, MagicMock())
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "HALF_OPEN"
        assert kwargs["ExpressionAttributeValues"][":expected"] == "OPEN"
        assert kwargs["ExpressionAttributeValues"][":ra"] == 2
        index_module.sns.publish.assert_called_once()

    def test_manual_probe_from_closed_is_noop(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "CLOSED"}
        }
        index_module.handler(
            {"action": "manual_probe", "user": "a", "reason": "r"}, MagicMock()
        )
        index_module.concurrency_table.update_item.assert_not_called()

    def test_unknown_action_returns_400(self, index_module):
        result = index_module.handler({"action": "bogus"}, MagicMock())
        assert result["statusCode"] == 400

    def test_broadcast_action_publishes_without_mutating_state(self, index_module):
        """The broadcast action must re-publish current state without writing DDB or SNS."""
        state_item = {
            "counter_id": "circuit_breaker",
            "state": "CLOSED",
            "failure_count": 0,
        }
        index_module.concurrency_table.get_item.return_value = {"Item": state_item}
        with patch.object(index_module, "publish_to_appsync") as mock_publish:
            result = index_module.handler({"action": "broadcast"}, MagicMock())
        assert result["statusCode"] == 200
        assert result["body"] == state_item
        mock_publish.assert_called_once_with(state_item)
        index_module.concurrency_table.update_item.assert_not_called()
        index_module.sns.publish.assert_not_called()
        index_module.cloudwatch.put_metric_data.assert_not_called()


class TestHalfOpenReopens:
    """HALF_OPEN -> OPEN when no probe closed the breaker in time (issue #1437).

    Before this transition existed the only automatic exit from HALF_OPEN was
    forward to CLOSED, which needs a workflow to *succeed*, so a half-open
    breaker whose probes all failed stayed half-open for the life of the
    outage and never re-timed its recovery timeout.
    """

    def test_the_window_is_long_enough_for_a_probe_to_finish(self, index_module):
        """The floor, not the exact value.

        A window shorter than a probe workflow can never close the breaker,
        because ``workflow_tracker`` closes it only while the state is still
        HALF_OPEN — so the direction that must be refused is *shortening*. A
        longer window is a trade, not a defect, which is why this is an
        inequality: pinning the number would just restate the assignment.
        """
        assert index_module.HALF_OPEN_TIMEOUT_SECONDS >= 900
        assert index_module.HALF_OPEN_TIMEOUT_SECONDS >= index_module.RECOVERY_TIMEOUT

    def test_an_episode_past_the_window_reopens(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {
                "state": "HALF_OPEN",
                "half_open_at": _ago(index_module.HALF_OPEN_TIMEOUT_SECONDS + 60),
            }
        }
        with patch.object(index_module, "publish_to_appsync") as appsync:
            index_module.handle_health_check()
        index_module.concurrency_table.update_item.assert_called()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "OPEN"
        # Conditional on still being HALF_OPEN, so two health checks racing
        # each other cannot both transition.
        assert kwargs["ExpressionAttributeValues"][":expected"] == "HALF_OPEN"
        assert kwargs["ConditionExpression"] == "#state = :expected"
        # The three fan-outs an operator learns about the transition through:
        # the alerts topic, the metric the dashboard and alarms read, and the
        # push that updates the UI badge without waiting for its poll.
        index_module.sns.publish.assert_called_once()
        sns_kwargs = index_module.sns.publish.call_args.kwargs
        assert "OPEN" in sns_kwargs["Subject"]
        assert '"circuit_breaker_state": "OPEN"' in sns_kwargs["Message"]
        index_module.cloudwatch.put_metric_data.assert_called_once()
        metric = index_module.cloudwatch.put_metric_data.call_args.kwargs[
            "MetricData"
        ][0]
        assert metric["MetricName"] == "CircuitBreakerOpened"
        assert metric["Value"] == 1
        # What it is handed is asserted against a real table in
        # TestHalfOpenReopensAgainstDynamoDB, where a read reflects the write.
        appsync.assert_called_once()

    @pytest.mark.parametrize("suffix", ["+00:00", "Z"])
    def test_either_spelling_of_UTC_in_the_episode_start_is_read(
        self, index_module, suffix
    ):
        """``Z`` and ``+00:00`` are the same instant, and only the second is
        what ``datetime.isoformat`` produces — so a record stamped by anything
        else must not be read as a naive timestamp, which raises rather than
        reopening."""
        started = datetime.now(timezone.utc) - timedelta(
            seconds=index_module.HALF_OPEN_TIMEOUT_SECONDS + 60
        )
        half_open_at = started.replace(tzinfo=None).isoformat() + suffix
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "HALF_OPEN", "half_open_at": half_open_at}
        }
        index_module.handle_health_check()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "OPEN"

    def test_reopening_re_times_the_recovery_timeout(self, index_module):
        """``opened_at`` must be rewritten, or the breaker reopens and
        immediately promotes itself back to HALF_OPEN on the next tick."""
        index_module.concurrency_table.get_item.return_value = {
            "Item": {
                "state": "HALF_OPEN",
                "opened_at": _ago(99999),
                "half_open_at": _ago(index_module.HALF_OPEN_TIMEOUT_SECONDS + 1),
            }
        }
        index_module.handle_health_check()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert "opened_at = :opened" in kwargs["UpdateExpression"]
        assert _seconds_since(kwargs["ExpressionAttributeValues"][":opened"]) < 30

    def test_an_episode_inside_the_window_changes_nothing(self, index_module):
        """The other half of the rule. A test written only for the transition
        above passes on a rule that reopens immediately, which would be worse
        than the defect: it would refuse every document for the whole outage."""
        index_module.concurrency_table.get_item.return_value = {
            "Item": {
                "state": "HALF_OPEN",
                "half_open_at": _ago(index_module.HALF_OPEN_TIMEOUT_SECONDS - 60),
            }
        }
        index_module.handle_health_check()
        index_module.concurrency_table.update_item.assert_not_called()
        index_module.sns.publish.assert_not_called()
        index_module.cloudwatch.put_metric_data.assert_not_called()

    def test_a_fresh_episode_changes_nothing(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "HALF_OPEN", "half_open_at": _ago(1)}
        }
        index_module.handle_health_check()
        index_module.concurrency_table.update_item.assert_not_called()

    def test_the_window_boundary_reopens(self, index_module):
        """``>=``, matching the OPEN -> HALF_OPEN comparison above it."""
        index_module.concurrency_table.get_item.return_value = {
            "Item": {
                "state": "HALF_OPEN",
                "half_open_at": _ago(index_module.HALF_OPEN_TIMEOUT_SECONDS),
            }
        }
        index_module.handle_health_check()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "OPEN"

    def test_a_record_with_no_episode_start_is_stamped_not_reopened(
        self, index_module
    ):
        """The first evaluation after deploy, when the record predates
        ``half_open_at``. A missing timestamp must read as "the episode starts
        now", not as the epoch: the epoch reading reopens a breaker that may be
        probing healthily, and the only cost of this direction is one cycle."""
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "HALF_OPEN"}
        }
        index_module.handle_health_check()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "HALF_OPEN", (
            "a record with no episode start must not be reopened on sight"
        )
        assert kwargs["ExpressionAttributeValues"][":expected"] == "HALF_OPEN"
        assert "half_open_at = :half_open_at" in kwargs["UpdateExpression"]
        assert (
            _seconds_since(kwargs["ExpressionAttributeValues"][":half_open_at"]) < 30
        )
        # Nothing about the state changed, so nothing is announced.
        index_module.sns.publish.assert_not_called()
        index_module.cloudwatch.put_metric_data.assert_not_called()

    def test_a_stale_last_checked_at_does_not_stand_in_for_the_episode_start(
        self, index_module
    ):
        """``last_checked_at`` is written by every transition and by the
        stamping write, so it is not the episode start and must not be read as
        one."""
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "HALF_OPEN", "last_checked_at": _ago(99999)}
        }
        index_module.handle_health_check()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "HALF_OPEN"

    def test_a_lost_race_publishes_nothing(self, index_module):
        index_module.concurrency_table.get_item.return_value = {
            "Item": {
                "state": "HALF_OPEN",
                "half_open_at": _ago(index_module.HALF_OPEN_TIMEOUT_SECONDS + 60),
            }
        }
        index_module.concurrency_table.update_item.side_effect = _cond_check_failed()
        index_module.handle_health_check()
        index_module.sns.publish.assert_not_called()
        index_module.cloudwatch.put_metric_data.assert_not_called()

    def test_a_closed_record_carrying_a_stale_episode_start_is_untouched(
        self, index_module
    ):
        """``workflow_tracker`` closes the breaker without clearing
        ``half_open_at``, so a CLOSED record keeps the start of the episode
        that closed it. The rule must key on the state, not on the age."""
        index_module.concurrency_table.get_item.return_value = {
            "Item": {
                "state": "CLOSED",
                "half_open_at": _ago(index_module.HALF_OPEN_TIMEOUT_SECONDS + 99999),
            }
        }
        index_module.handle_health_check()
        index_module.concurrency_table.update_item.assert_not_called()

    def test_reopening_leaves_the_counters_alone(self, index_module):
        """``failure_count`` counts Bedrock outages the alarm detected, and
        ``recovery_attempts`` is incremented by the OPEN -> HALF_OPEN
        transition that starts the next episode. Incrementing either here
        would make them a function of how long the outage lasted."""
        index_module.concurrency_table.get_item.return_value = {
            "Item": {
                "state": "HALF_OPEN",
                "failure_count": 4,
                "recovery_attempts": 2,
                "half_open_at": _ago(index_module.HALF_OPEN_TIMEOUT_SECONDS + 60),
            }
        }
        index_module.handle_health_check()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert ":fc" not in kwargs["ExpressionAttributeValues"]
        assert ":ra" not in kwargs["ExpressionAttributeValues"]
        assert "failure_count" not in kwargs["UpdateExpression"]
        assert "recovery_attempts" not in kwargs["UpdateExpression"]
        # The reason reaches the UI's lastError field and the SNS message.
        assert "HALF_OPEN" in kwargs["ExpressionAttributeValues"][":err"]


class TestExistingTransitionsStillRecordTheEpisodeStart:
    """Every entry into HALF_OPEN must stamp ``half_open_at``.

    All three go through ``update_circuit_breaker_state``, which is what makes
    this true by construction rather than by three call sites remembering. If a
    fourth entry is ever added that bypasses it, the record will carry a stale
    episode start and the rule above will reopen the breaker on the first tick.
    """

    @staticmethod
    def _health_check(module):
        module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN", "opened_at": _ago(400), "recovery_attempts": 2}
        }
        module.handle_health_check()

    @staticmethod
    def _alarm_ok(module):
        module.concurrency_table.get_item.return_value = {"Item": {"state": "OPEN"}}
        module.handle_alarm_event("OK", "alarm cleared")

    @staticmethod
    def _manual_probe(module):
        module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN", "recovery_attempts": 1}
        }
        module.handler(
            {"action": "manual_probe", "user": "admin@x", "reason": "test"},
            MagicMock(),
        )

    @pytest.mark.parametrize(
        "transition", ["_health_check", "_alarm_ok", "_manual_probe"]
    )
    def test_the_entry_records_a_fresh_episode_start(self, index_module, transition):
        getattr(self, transition)(index_module)
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == "HALF_OPEN"
        assert "half_open_at = :half_open_at" in kwargs["UpdateExpression"]
        assert (
            _seconds_since(kwargs["ExpressionAttributeValues"][":half_open_at"]) < 30
        )


class TestHealthCheckTransitionTable:
    """Every health-check outcome in one place, so the new rule cannot drift
    the three transitions that were already here."""

    @pytest.mark.parametrize(
        "item,expected_write",
        [
            ({"state": "CLOSED"}, None),
            ({"state": "OPEN", "opened_at": _ago(30)}, None),
            ({"state": "OPEN"}, None),
            ({"state": "OPEN", "opened_at": _ago(400)}, "HALF_OPEN"),
            ({"state": "HALF_OPEN", "half_open_at": _ago(60)}, None),
            ({"state": "HALF_OPEN", "half_open_at": _ago(99999)}, "OPEN"),
            ({"state": "HALF_OPEN"}, "HALF_OPEN"),
        ],
    )
    def test_the_outcome(self, index_module, item, expected_write):
        index_module.concurrency_table.get_item.return_value = {"Item": item}
        index_module.handle_health_check()
        if expected_write is None:
            index_module.concurrency_table.update_item.assert_not_called()
            return
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":state"] == expected_write


class TestHalfOpenReopensAgainstDynamoDB:
    """The parts a MagicMock table cannot show: what is persisted across a
    whole cycle, and that a conditional write makes one transition out of
    several concurrent attempts."""

    def test_the_cycle_reopens_and_then_re_times_the_recovery_timeout(
        self, ddb_module
    ):
        """OPEN -> HALF_OPEN -> OPEN -> (waits) -> HALF_OPEN, driven through
        DynamoDB so each step reads what the last one wrote.

        The second OPEN is the fix. The health check immediately after it must
        leave the breaker OPEN — that is the re-timed recovery timeout, and it
        is what restores ``extend_visibility_for_outage`` for every refused
        message (see ``queue_processor``'s
        ``test_an_open_refusal_extends_visibility_to_the_recovery_timeout``).
        """
        _put_breaker(
            ddb_module, state="OPEN", opened_at=_ago(400), recovery_attempts=0
        )

        ddb_module.handle_health_check()
        item = _breaker_item(ddb_module)
        assert item["state"] == "HALF_OPEN"
        assert int(item["recovery_attempts"]) == 1
        assert _seconds_since(item["half_open_at"]) < 30

        # Nothing succeeds. Rewind the episode start past the window, which is
        # all a long failed episode amounts to.
        ddb_module.concurrency_table.update_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID},
            UpdateExpression="SET half_open_at = :t",
            ExpressionAttributeValues={
                ":t": _ago(ddb_module.HALF_OPEN_TIMEOUT_SECONDS + 60)
            },
        )
        with patch.object(ddb_module, "publish_to_appsync") as appsync:
            ddb_module.handle_health_check()
        item = _breaker_item(ddb_module)
        assert item["state"] == "OPEN", (
            "a half-open episode that produced no successful probe must reopen"
        )
        assert _seconds_since(item["opened_at"]) < 30, (
            "the recovery timeout must be re-timed from the reopen"
        )
        assert "half_open_at" not in item, (
            "the episode is over, so its start must not be left behind"
        )
        assert appsync.call_args.args[0]["state"] == "OPEN", (
            "the UI is pushed the state that was just written, not the one "
            "that was read before the write"
        )

        # Freshly OPEN, so the next tick must not promote it straight back.
        ddb_module.handle_health_check()
        assert _breaker_item(ddb_module)["state"] == "OPEN"

        # And once the recovery timeout has elapsed, it probes again, with a
        # fresh episode start and the attempt counted.
        ddb_module.concurrency_table.update_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID},
            UpdateExpression="SET opened_at = :t",
            ExpressionAttributeValues={":t": _ago(400)},
        )
        ddb_module.handle_health_check()
        item = _breaker_item(ddb_module)
        assert item["state"] == "HALF_OPEN"
        assert int(item["recovery_attempts"]) == 2
        assert _seconds_since(item["half_open_at"]) < 30

    def test_a_successful_probe_closes_and_the_rule_cannot_reopen_it(
        self, ddb_module
    ):
        """``workflow_tracker.notify_circuit_breaker_success`` closes the
        breaker with a conditional write of its own and does not clear
        ``half_open_at``. The CLOSED record therefore carries an episode start
        that is older than the window from the moment it is written, and the
        health check must still leave it alone."""
        _put_breaker(
            ddb_module,
            state="HALF_OPEN",
            half_open_at=_ago(ddb_module.HALF_OPEN_TIMEOUT_SECONDS + 60),
        )
        # The same write workflow_tracker issues on a successful workflow.
        ddb_module.concurrency_table.update_item(
            Key={"counter_id": ddb_module.CIRCUIT_BREAKER_ID},
            UpdateExpression=(
                "SET #state = :closed, last_checked_at = :ts "
                "REMOVE last_error, opened_at"
            ),
            ConditionExpression="#state = :expected",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":closed": "CLOSED",
                ":expected": "HALF_OPEN",
                ":ts": _ago(0),
            },
        )
        ddb_module.handle_health_check()
        assert _breaker_item(ddb_module)["state"] == "CLOSED"
        ddb_module.sns.publish.assert_not_called()

    def test_reopening_leaves_the_probe_budget_attributes_alone(self, ddb_module):
        """``half_open_probes`` / ``half_open_probe_window`` belong to the
        queue processor's rolling-window budget (#1435). The reopen writes
        named attributes only, so the window number is left to be replaced by
        the next episode rather than cleaned up here."""
        _put_breaker(
            ddb_module,
            state="HALF_OPEN",
            half_open_at=_ago(ddb_module.HALF_OPEN_TIMEOUT_SECONDS + 60),
            half_open_probes=5,
            half_open_probe_window=12345,
        )
        ddb_module.handle_health_check()
        item = _breaker_item(ddb_module)
        assert item["state"] == "OPEN"
        assert int(item["half_open_probes"]) == 5
        assert int(item["half_open_probe_window"]) == 12345

    def test_concurrent_health_checks_make_one_transition(self, ddb_module):
        """The race, driven rather than sequenced.

        The health check is a 5-minute schedule, but EventBridge is
        at-least-once and a manual invocation can land beside a scheduled one,
        so two evaluations can read HALF_OPEN before either writes. A barrier
        on the state read holds every caller until all of them have read, which
        is the interleaving a sequence of calls never produces. An
        unconditional write reopens the breaker ``callers`` times, publishing
        ``callers`` notifications and rewriting ``opened_at`` each time — which
        would extend the outage pause by one window per duplicate.

        ⚠️ The ``UpdateItem`` calls are serialized by a lock, and that models
        DynamoDB rather than weakening the test. DynamoDB evaluates a
        ``ConditionExpression`` and applies its update as one serialized
        operation per item; ``moto`` read-modify-writes its backing store with
        no lock, so without this two writers can both see HALF_OPEN and both
        "pass" the condition, and the test measures ``moto``'s thread safety
        instead of this code's atomicity.
        """
        callers = 4
        _put_breaker(
            ddb_module,
            state="HALF_OPEN",
            half_open_at=_ago(ddb_module.HALF_OPEN_TIMEOUT_SECONDS + 60),
        )

        all_have_read = threading.Barrier(callers, timeout=30)
        real_get_item = ddb_module.concurrency_table.get_item
        real_update_item = ddb_module.concurrency_table.update_item
        item_lock = threading.Lock()

        def get_item_then_wait(**kwargs):
            response = real_get_item(**kwargs)
            # Only the first read of each caller sees HALF_OPEN; the winner's
            # second read (for the AppSync fan-out) sees OPEN and must not
            # wait on a barrier the losers have already left.
            if (response.get("Item") or {}).get("state") == ddb_module.STATE_HALF_OPEN:
                all_have_read.wait()
            return response

        def update_item_serialized(**kwargs):
            with item_lock:
                return real_update_item(**kwargs)

        ddb_module.concurrency_table.get_item = get_item_then_wait
        ddb_module.concurrency_table.update_item = update_item_serialized
        try:
            threads = [
                threading.Thread(target=ddb_module.handle_health_check)
                for _ in range(callers)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                assert not thread.is_alive(), "a caller never returned"
        finally:
            ddb_module.concurrency_table.get_item = real_get_item
            ddb_module.concurrency_table.update_item = real_update_item

        assert _breaker_item(ddb_module)["state"] == "OPEN"
        assert ddb_module.sns.publish.call_count == 1, (
            f"{callers} concurrent health checks that all read HALF_OPEN before "
            f"any of them wrote announced "
            f"{ddb_module.sns.publish.call_count} reopens; the transition is "
            f"not conditional"
        )
        assert ddb_module.cloudwatch.put_metric_data.call_count == 1
