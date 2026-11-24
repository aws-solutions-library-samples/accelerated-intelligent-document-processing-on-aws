# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review Routing (``hitl.enabled``) of the active profile, for Review Management.

``getMyReviewQueues`` returns it so the page can warn while routing rules are not
evaluated. What is pinned: the value is the active profile's switch, ``None`` is
"could not be read" and never "off", and ``get_my_queues`` passes it on as read.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.config.active_profile import ActiveProfileValue
from idp_common.config.models import IDPConfig
from idp_common.review_queues import review_routing_setting
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.models import CallerContext
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore

pytestmark = pytest.mark.unit

_MANAGER = "idp_common.config.configuration_manager.ConfigurationManager"
ADMIN = CallerContext(
    reviewer_key="admin@example.com",
    email="admin@example.com",
    groups=frozenset({"Admin"}),
    is_admin=True,
)


@pytest.fixture
def fresh_provider(monkeypatch):
    """No reader left from another test: the provider is built anew."""
    monkeypatch.setattr(review_routing_setting, "_PROVIDER", None)


def _read(config, monkeypatch):
    """What the provider answers when the active profile is ``config``."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "config-table")
    with patch(_MANAGER) as manager_cls:
        manager = manager_cls.return_value
        manager.resolve_active_version.return_value = "lending"
        manager.get_merged_configuration.return_value = config
        value = review_routing_setting.review_routing_enabled_provider().get()
        manager.get_merged_configuration.assert_called_once_with("lending")
    return value


class TestTheProvider:
    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({"hitl": {"enabled": True}}, True),
            ({"hitl": {"enabled": False}}, False),
            ({"hitl": {"enabled": "true"}}, True),
            # Unset reads as the model default, which is off.
            ({"hitl": {}}, False),
            ({}, False),
            (IDPConfig.model_validate({"hitl": {"enabled": True}}), True),
            (IDPConfig(), False),
        ],
    )
    def test_it_is_the_active_profiles_switch(
        self, fresh_provider, monkeypatch, config, expected
    ):
        assert _read(config, monkeypatch) is expected

    def test_a_table_that_is_not_configured_is_not_known(
        self, fresh_provider, monkeypatch
    ):
        monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)

        assert review_routing_setting.review_routing_enabled_provider().get() is None

    def test_a_profile_that_is_not_there_is_not_known(
        self, fresh_provider, monkeypatch
    ):
        assert _read(None, monkeypatch) is None

    def test_a_read_that_fails_is_not_known_and_is_logged(
        self, fresh_provider, monkeypatch, caplog
    ):
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "config-table")

        with (
            patch(_MANAGER, side_effect=RuntimeError("throttled")),
            caplog.at_level("WARNING"),
        ):
            value = review_routing_setting.review_routing_enabled_provider().get()

        # "Could not be read" is not "off": the page shows no warning for it.
        assert value is None
        assert [r.getMessage() for r in caplog.records] == [
            "event=active_profile_setting_fallback setting=hitl.enabled "
            "reason=RuntimeError"
        ]

    def test_it_is_one_cached_reader_per_container(self, fresh_provider, monkeypatch):
        first = review_routing_setting.review_routing_enabled_provider()

        assert isinstance(first, ActiveProfileValue)
        assert review_routing_setting.review_routing_enabled_provider() is first
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "config-table")
        with patch(_MANAGER) as manager_cls:
            manager = manager_cls.return_value
            manager.resolve_active_version.return_value = "lending"
            manager.get_merged_configuration.return_value = {"hitl": {"enabled": True}}

            assert (first.get(), first.get(), first.get()) == (True, True, True)

            # Read once: the next calls are served from the cache.
            assert manager.get_merged_configuration.call_count == 1


class _Stub:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class TestMyQueuesCarryIt:
    @pytest.fixture
    def service(self, review_table, monkeypatch):
        monkeypatch.setenv("HITL_TABLE_NAME", review_table)
        return ReviewQueueService(
            ReviewStore(review_table), QueueDirectory(), notifier=MagicMock(), s3=None
        )

    @pytest.mark.parametrize("value", [True, False, None])
    def test_the_value_is_passed_on_as_it_was_read(self, service, monkeypatch, value):
        monkeypatch.setattr(review_routing_setting, "_PROVIDER", _Stub(value))

        result = service.get_my_queues(ADMIN)

        assert result.review_routing_enabled is value
        assert result.dump()["reviewRoutingEnabled"] is value

    def test_it_is_apart_from_the_default_queues_switch(self, service, monkeypatch):
        """Two causes of "routing rules are not evaluated", told apart."""
        monkeypatch.setattr(review_routing_setting, "_PROVIDER", _Stub(False))

        result = service.get_my_queues(ADMIN)

        assert (result.review_routing_enabled, result.default_queue_enabled) == (
            False,
            True,
        )
