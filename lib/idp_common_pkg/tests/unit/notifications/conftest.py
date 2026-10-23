# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fixtures for the ``idp_common.notifications`` suite."""

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from idp_common.notifications.catalogue import (  # noqa: E402
    register_type,
    unregister_type,
)
from idp_common.notifications.models import NotificationType  # noqa: E402

# The table definition lives in _table.py so suites outside this folder can
# build the same table; test_table_parity.py pins it to the parent template.
from ._table import REGION, create_notifications_table  # noqa: E402

TEST_TYPE = NotificationType(
    id="test.sample", display_name="Sample", description="Test-only type"
)


@pytest.fixture
def notifications_table():
    """A moto NotificationsTable; yields its name."""
    with mock_aws():
        yield create_notifications_table()


@pytest.fixture
def test_type():
    """Register the test-only type for one test."""
    register_type(TEST_TYPE)
    yield TEST_TYPE
    unregister_type(TEST_TYPE.id)


class FakeClock:
    """Deterministic UTC clock that advances ``step`` on every read."""

    def __init__(self, start=None, step_ms: int = 1):
        from datetime import datetime, timedelta, timezone

        self.now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.step = timedelta(milliseconds=step_ms)
        self.reads: list = []

    def __call__(self):
        self.now = self.now + self.step
        self.reads.append(self.now)
        return self.now

    def advance(self, **kwargs):
        from datetime import timedelta

        self.now = self.now + timedelta(**kwargs)

    @property
    def epoch(self) -> int:
        return int(self.now.timestamp())


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_service(notifications_table, clock):
    """Factory for a NotificationService on the moto table with fixed settings."""
    from idp_common.notifications.models import NotificationSettings
    from idp_common.notifications.service import NotificationService
    from idp_common.notifications.store import NotificationStore

    def _make(
        *,
        retention_days: int = 30,
        max_per_user: int = 50,
        group_lister=None,
        user_pool_id=None,
    ):
        store = NotificationStore(table_name=notifications_table, region=REGION)
        settings = NotificationSettings(
            retention_days=retention_days, max_per_user=max_per_user
        )
        return NotificationService(
            store=store,
            user_pool_id=user_pool_id,
            region=REGION,
            settings_provider=lambda: settings,
            group_lister=group_lister,
            clock=clock,
        )

    return _make


def message_items(store, rk: str) -> list[dict]:
    """Every ``N#`` item in a recipient's partition, ascending."""
    from idp_common.notifications.keys import partition_key

    items: list[dict] = []
    for page in store.client.query_pages(
        "PK = :pk AND begins_with(SK, :n)",
        expression_attribute_values={":pk": partition_key(rk), ":n": "N#"},
        consistent_read=True,
    ):
        items.extend(page.get("Items", []))
    return items


def put_raw_message(
    store, rk: str, msg_id: str, *, expires_after: int, read=False, **extra
):
    """Insert a message item directly (bypassing post)."""
    from idp_common.notifications.keys import message_sk, partition_key

    pk = partition_key(rk)
    item = {
        "PK": pk,
        "SK": message_sk(msg_id),
        "Type": extra.pop("Type", "test.sample"),
        "Title": "raw",
        "Body": "",
        "Severity": extra.pop("Severity", "info"),
        "Source": "tests",
        "CreatedAt": "2026-01-01T00:00:00.000Z",
        "ExpiresAfter": expires_after,
        **extra,
    }
    if read:
        item["ReadAt"] = "2026-01-01T00:00:01.000Z"
    else:
        item["UnreadPK"] = pk
    store.client.put_item(item)
    return item
