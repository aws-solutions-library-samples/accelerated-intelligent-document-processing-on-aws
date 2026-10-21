# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The factory's headless no-op, and notify()'s never-raise contract."""

import logging
from unittest import mock

import pytest

from idp_common.dynamodb.client import DynamoDBError
from idp_common.notifications import service as service_module
from idp_common.notifications.service import create_notification_service, notify

ALICE = "alice@example.com"
_LOGGER = "idp_common.notifications.service"


@pytest.fixture(autouse=True)
def _fresh_default_service():
    service_module._default_service.cache_clear()
    yield
    service_module._default_service.cache_clear()


def _kwargs(**overrides):
    base = {"type_id": "test.sample", "title": "t", "source": "tests", "users": [ALICE]}
    base.update(overrides)
    return base


@pytest.mark.unit
class TestFactory:
    def test_none_when_table_unset(self, monkeypatch, caplog):
        monkeypatch.setenv("NOTIFICATIONS_TABLE_NAME", "")
        caplog.set_level(logging.DEBUG, logger=_LOGGER)
        assert create_notification_service() is None
        assert any("Inbox disabled" in r.message for r in caplog.records)

    def test_builds_from_env(self, monkeypatch, notifications_table):
        monkeypatch.setenv("NOTIFICATIONS_TABLE_NAME", notifications_table)
        monkeypatch.setenv("USER_POOL_ID", "us-east-1_X")
        svc = create_notification_service(region="us-east-1")
        assert svc is not None
        assert svc.user_pool_id == "us-east-1_X"
        assert svc.store.client.table_name == notifications_table


@pytest.mark.unit
class TestNotify:
    def test_no_service_returns_none_and_logs_debug(self, monkeypatch, caplog):
        monkeypatch.setenv("NOTIFICATIONS_TABLE_NAME", "")
        caplog.set_level(logging.DEBUG, logger=_LOGGER)
        with mock.patch.object(service_module, "put_metric") as metric:
            assert notify(**_kwargs()) is None
        metric.assert_not_called()
        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("skipped" in r.message for r in debug)

    def test_ddb_failure_is_reported_not_raised(self, make_service, test_type, caplog):
        caplog.set_level(logging.ERROR, logger=_LOGGER)
        svc = make_service()
        with (
            mock.patch.object(
                svc.store, "put_message", side_effect=DynamoDBError("down", "X")
            ),
            mock.patch.object(service_module, "put_metric") as metric,
        ):
            result = notify(**_kwargs(), service=svc)
        assert result is not None and result.failed == 1
        metric.assert_called_once_with("NotificationPostFailures", 1)
        assert any("inbox_delivery_failed" in r.message for r in caplog.records)

    def test_unexpected_exception_returns_none(self, make_service, test_type, caplog):
        caplog.set_level(logging.ERROR, logger=_LOGGER)
        svc = make_service()
        with (
            mock.patch.object(svc, "post", side_effect=RuntimeError("boom")),
            mock.patch.object(service_module, "put_metric") as metric,
        ):
            assert notify(**_kwargs(), service=svc) is None
        metric.assert_called_once_with("NotificationPostFailures", 1)
        failures = [r for r in caplog.records if "inbox_post_failed" in r.message]
        assert failures and failures[0].exc_info is not None

    def test_invalid_input_never_raises(self, make_service, test_type, caplog):
        caplog.set_level(logging.ERROR, logger=_LOGGER)
        svc = make_service()
        with mock.patch.object(service_module, "put_metric") as metric:
            assert notify(**_kwargs(title="x" * 500), service=svc) is None
        metric.assert_called_once()
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_success_returns_result(self, make_service, test_type):
        svc = make_service()
        with mock.patch.object(service_module, "put_metric") as metric:
            result = notify(**_kwargs(), service=svc)
        assert result is not None and result.delivered == 1
        metric.assert_not_called()
