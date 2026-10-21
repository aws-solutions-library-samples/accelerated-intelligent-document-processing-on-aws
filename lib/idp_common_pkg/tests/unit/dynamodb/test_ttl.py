# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Module-level `calculate_ttl` and the service method that delegates to it."""

from datetime import datetime, timezone
from unittest import mock

import pytest

from idp_common.dynamodb import calculate_ttl

FIXED = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXED_EPOCH = int(FIXED.timestamp())


@pytest.mark.unit
class TestCalculateTtl:
    def test_thirty_days(self):
        assert calculate_ttl(30, now=FIXED) == FIXED_EPOCH + 2_592_000

    def test_fractional_days(self):
        assert calculate_ttl(0.5, now=FIXED) == FIXED_EPOCH + 43_200

    def test_naive_now_is_utc(self):
        naive = datetime(2026, 1, 1)
        assert calculate_ttl(1, now=naive) == FIXED_EPOCH + 86_400

    def test_default_now_is_current_time(self):
        before = int(datetime.now(timezone.utc).timestamp())
        value = calculate_ttl(1)
        after = int(datetime.now(timezone.utc).timestamp())
        assert before + 86_400 <= value <= after + 86_400


@pytest.mark.unit
class TestServiceDelegates:
    def test_service_method_calls_helper(self):
        from idp_common.dynamodb.service import DocumentDynamoDBService

        svc = DocumentDynamoDBService.__new__(DocumentDynamoDBService)
        with mock.patch(
            "idp_common.dynamodb.service.calculate_ttl", return_value=123
        ) as helper:
            assert svc.calculate_ttl(7) == 123
        helper.assert_called_once_with(7)
