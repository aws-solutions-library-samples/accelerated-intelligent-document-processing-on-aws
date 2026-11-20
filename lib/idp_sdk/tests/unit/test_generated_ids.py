# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generated ids must not collide when the clock does not move (#2113).

A generated batch id is the S3 key prefix its documents are copied under, so two
ids that compare equal are two batches overwriting each other. The clock is frozen
here rather than relied on: two calls a few microseconds apart almost always fall
in the same second anyway, but a test that only usually exercises the collision
passes on a timestamp-only id whenever it happens to straddle a second boundary.
"""

import re
from datetime import datetime, timezone

import pytest

import idp_sdk
from idp_sdk._core import ids

FROZEN_INSTANT = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
ID_SHAPE = re.compile(r"(?P<prefix>.+)-(?P<ts>\d{8}-\d{6})-(?P<suffix>[0-9a-f]{8})")


class _FrozenDatetime(datetime):
    """`datetime` whose `now` always returns `FROZEN_INSTANT`, in the zone asked."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_INSTANT if tz is None else FROZEN_INSTANT.astimezone(tz)


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(ids, "datetime", _FrozenDatetime)


@pytest.mark.unit
class TestGenerateTimestampedId:
    def test_two_ids_from_the_same_instant_differ(self, frozen_clock):
        first = ids.generate_timestamped_id("cli-batch")
        second = ids.generate_timestamped_id("cli-batch")
        assert first != second
        # Both carry the frozen second, so the difference is the suffix alone.
        assert first.rsplit("-", 1)[0] == second.rsplit("-", 1)[0]

    def test_many_ids_from_the_same_instant_are_all_distinct(self, frozen_clock):
        generated = {ids.generate_timestamped_id("p") for _ in range(1000)}
        assert len(generated) == 1000

    def test_the_shape_is_prefix_utc_timestamp_and_eight_hex(self, frozen_clock):
        match = ID_SHAPE.fullmatch(ids.generate_timestamped_id("experiment-v1"))
        assert match, "id does not have the documented shape"
        assert match["prefix"] == "experiment-v1"
        assert match["ts"] == "20260102-030405"

    def test_the_timestamp_is_utc_whatever_the_local_zone(self, monkeypatch):
        """The helper must ask for UTC. A naive `now()` would render local time."""
        calls = []

        class _Recording(_FrozenDatetime):
            @classmethod
            def now(cls, tz=None):
                calls.append(tz)
                return super().now(tz)

        monkeypatch.setattr(ids, "datetime", _Recording)
        ids.generate_timestamped_id("p")
        assert calls == [timezone.utc]

    def test_the_suffix_comes_from_the_random_source_not_the_clock(
        self, frozen_clock, monkeypatch
    ):
        """Freezing `datetime` alone would pass a suffix derived from another clock
        (`time.time_ns()`, a pid and a counter), which two processes at the same
        instant can share. Pin the suffix to `secrets.token_hex` itself."""
        requested = []

        def fake_token_hex(nbytes):
            requested.append(nbytes)
            return "deadbeef"

        monkeypatch.setattr(ids.secrets, "token_hex", fake_token_hex)
        assert ids.generate_timestamped_id("p") == "p-20260102-030405-deadbeef"
        assert requested == [4]

    def test_it_is_exported_from_the_top_level_package(self):
        assert idp_sdk.generate_timestamped_id is ids.generate_timestamped_id
        assert "generate_timestamped_id" in idp_sdk.__all__
