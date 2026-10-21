# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Recipient expansion: Cognito pagination, disabled users, de-dup, the bound."""

import logging

import boto3
import pytest
from botocore.stub import Stubber

from idp_common.notifications.exceptions import NotificationValidationError
from idp_common.notifications.recipients import (
    expand_recipients,
    list_group_members,
)

POOL = "us-east-1_TESTPOOL"


def _user(email, enabled=True, username=None):
    return {
        "Username": username or email,
        "Enabled": enabled,
        "Attributes": [{"Name": "email", "Value": email}],
    }


@pytest.mark.unit
class TestListGroupMembers:
    def test_paginates_with_limit_60(self):
        client = boto3.client("cognito-idp", region_name="us-east-1")
        stubber = Stubber(client)
        stubber.add_response(
            "list_users_in_group",
            {"Users": [_user("a@x.com")], "NextToken": "page2"},
            {"UserPoolId": POOL, "GroupName": "Reviewer", "Limit": 60},
        )
        stubber.add_response(
            "list_users_in_group",
            {"Users": [_user("b@x.com")]},
            {
                "UserPoolId": POOL,
                "GroupName": "Reviewer",
                "Limit": 60,
                "NextToken": "page2",
            },
        )
        with stubber:
            users = list(list_group_members(POOL, "Reviewer", client=client))
        stubber.assert_no_pending_responses()
        assert [u["Username"] for u in users] == ["a@x.com", "b@x.com"]


def _lister(groups):
    def lister(pool, group):
        assert pool == POOL
        return iter(groups[group])

    return lister


@pytest.mark.unit
class TestExpand:
    def test_disabled_users_are_skipped(self):
        keys, truncated = expand_recipients(
            [],
            ["G"],
            user_pool_id=POOL,
            lister=_lister({"G": [_user("a@x.com"), _user("b@x.com", enabled=False)]}),
        )
        assert keys == ["a@x.com"] and truncated is False

    def test_dedupe_across_users_and_groups_keeps_order(self):
        keys, _ = expand_recipients(
            ["c@x.com", "a@x.com"],
            ["G1", "G2"],
            user_pool_id=POOL,
            lister=_lister(
                {
                    "G1": [_user("a@x.com"), _user("b@x.com")],
                    "G2": [_user("b@x.com"), _user("d@x.com")],
                }
            ),
        )
        assert keys == ["c@x.com", "a@x.com", "b@x.com", "d@x.com"]

    def test_case_insensitive_dedupe(self):
        keys, _ = expand_recipients(
            ["Alice@x.com"],
            ["G"],
            user_pool_id=POOL,
            lister=_lister({"G": [_user("alice@x.com")]}),
        )
        assert keys == ["alice@x.com"]

    def test_truncation(self, caplog):
        caplog.set_level(logging.WARNING, logger="idp_common.notifications.recipients")
        keys, truncated = expand_recipients(
            ["u1"],
            ["G"],
            user_pool_id=POOL,
            lister=_lister({"G": [_user(f"m{i}@x.com") for i in range(10)]}),
            max_recipients=3,
        )
        assert keys == ["u1", "m0@x.com", "m1@x.com"]
        assert truncated is True
        assert any(
            "event=inbox_fanout_truncated" in r.message and "cap=3" in r.message
            for r in caplog.records
        )

    def test_exactly_at_cap_is_not_truncated(self):
        keys, truncated = expand_recipients(
            ["a", "b", "c"], [], user_pool_id=None, max_recipients=3
        )
        assert keys == ["a", "b", "c"] and truncated is False

    def test_groups_without_pool_raise(self):
        with pytest.raises(NotificationValidationError):
            expand_recipients([], ["G"], user_pool_id=None)
