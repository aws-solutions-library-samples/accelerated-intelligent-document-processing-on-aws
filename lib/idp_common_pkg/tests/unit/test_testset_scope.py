# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for per-user test-set scope (``allowedTestSets``).

This is a security boundary: sharing an annotation-queue link is only safe because
the server refuses out-of-scope access on every operation. These tests are written
adversarially — most are attempts to reach a test set the caller was not assigned —
and assert the *deny* direction, including the states where a naive implementation
would fail open (no scope recorded, lookup error, unknown role).
"""

import time
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from idp_common import testset_scope
from idp_common.testset_scope import (
    TestSetAccessDenied,
    assert_can_access_test_set,
    get_allowed_test_sets,
)

pytestmark = pytest.mark.unit


def _event(groups, email="user@example.com"):
    """An AppSync-shaped event for a Cognito caller."""
    return {
        "identity": {
            "claims": {"cognito:groups": groups, "email": email},
            "username": email,
        }
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    """Scope is cached per container; a stale entry would mask a test's setup."""
    testset_scope.clear_scope_cache()
    yield
    testset_scope.clear_scope_cache()


@pytest.fixture
def users_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="users",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "EmailIndex",
                    "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


def _seed_user(table, email, persona="Annotator", allowed=None):
    item = {
        "PK": f"USER#{email}",
        "SK": f"USER#{email}",
        "userId": email,
        "email": email,
        "persona": persona,
    }
    if allowed is not None:
        item["allowedTestSets"] = allowed
    table.put_item(Item=item)


class TestUnscopedRoles:
    def test_admin_and_author_are_unscoped(self, users_table):
        """They own the test sets; scoping them would break management."""
        for group in ("Admin", "Author"):
            assert_can_access_test_set(
                _event([group]), "any-set", users_table
            )  # does not raise

    def test_direct_invoke_is_trusted(self, users_table):
        """No Cognito identity = IAM-gated service call, per the repo convention."""
        assert_can_access_test_set({}, "any-set", users_table)
        assert_can_access_test_set({"identity": None}, "any-set", users_table)


class TestAnnotatorScope:
    def test_annotator_can_access_its_assigned_set(self, users_table):
        _seed_user(users_table, "ann@example.com", allowed=["ts-alpha"])
        assert_can_access_test_set(
            _event(["Annotator"], "ann@example.com"), "ts-alpha", users_table
        )

    def test_annotator_cannot_access_another_set(self, users_table):
        """The core boundary: assignment to one set grants nothing elsewhere."""
        _seed_user(users_table, "ann@example.com", allowed=["ts-alpha"])
        with pytest.raises(TestSetAccessDenied, match="ts-beta"):
            assert_can_access_test_set(
                _event(["Annotator"], "ann@example.com"), "ts-beta", users_table
            )

    def test_annotator_with_no_scope_is_denied_everything(self, users_table):
        """Fails closed.

        An annotator whose scope was never set — half-finished onboarding, or a
        revoked assignment — must be denied, not handed unrestricted access.
        """
        _seed_user(users_table, "ann@example.com", allowed=None)
        with pytest.raises(TestSetAccessDenied, match="not assigned to any test set"):
            assert_can_access_test_set(
                _event(["Annotator"], "ann@example.com"), "ts-alpha", users_table
            )

    def test_annotator_with_empty_scope_is_denied(self, users_table):
        _seed_user(users_table, "ann@example.com", allowed=[])
        with pytest.raises(TestSetAccessDenied):
            assert_can_access_test_set(
                _event(["Annotator"], "ann@example.com"), "ts-alpha", users_table
            )

    def test_annotator_with_no_user_record_is_denied(self, users_table):
        """A Cognito user with no DynamoDB record has no resolvable scope."""
        with pytest.raises(TestSetAccessDenied):
            assert_can_access_test_set(
                _event(["Annotator"], "ghost@example.com"), "ts-alpha", users_table
            )

    def test_multiple_assigned_sets_all_work(self, users_table):
        _seed_user(users_table, "ann@example.com", allowed=["ts-a", "ts-b"])
        event = _event(["Annotator"], "ann@example.com")
        assert_can_access_test_set(event, "ts-a", users_table)
        assert_can_access_test_set(event, "ts-b", users_table)
        with pytest.raises(TestSetAccessDenied):
            assert_can_access_test_set(event, "ts-c", users_table)


class TestOtherRoles:
    def test_reviewer_gets_no_test_set_access(self, users_table):
        """Production HITL review is a different axis and grants nothing here.

        A Reviewer can review production documents; that must not imply access to
        ground-truth test sets.
        """
        with pytest.raises(TestSetAccessDenied, match="requires Admin, Author"):
            assert_can_access_test_set(
                _event(["Reviewer"], "rev@example.com"), "ts-alpha", users_table
            )

    def test_viewer_gets_no_test_set_access(self, users_table):
        with pytest.raises(TestSetAccessDenied):
            assert_can_access_test_set(
                _event(["Viewer"], "v@example.com"), "ts-alpha", users_table
            )

    def test_no_groups_at_all_is_denied(self, users_table):
        with pytest.raises(TestSetAccessDenied):
            assert_can_access_test_set(
                _event([], "nobody@example.com"), "ts-alpha", users_table
            )

    def test_annotator_who_is_also_admin_is_unscoped(self, users_table):
        """Group union, not intersection: an admin who also annotates is an admin."""
        _seed_user(users_table, "both@example.com", allowed=["ts-alpha"])
        assert_can_access_test_set(
            _event(["Admin", "Annotator"], "both@example.com"), "ts-other", users_table
        )


class TestScopeLookup:
    def test_string_group_claim_is_normalized(self, users_table):
        """Cognito sends a bare string when the user is in exactly one group."""
        _seed_user(users_table, "ann@example.com", allowed=["ts-alpha"])
        event = {
            "identity": {
                "claims": {
                    "cognito:groups": "Annotator",
                    "email": "ann@example.com",
                }
            }
        }
        assert_can_access_test_set(event, "ts-alpha", users_table)

    def test_lookup_failure_denies_an_annotator(self, users_table):
        """An unreadable users table must not grant access.

        A caught lookup error must remove access rather than leaving the caller
        silently unrestricted.
        """

        class Broken:
            def query(self, **kwargs):
                raise RuntimeError("AccessDeniedException")

        with pytest.raises(TestSetAccessDenied):
            assert_can_access_test_set(
                _event(["Annotator"], "ann@example.com"), "ts-alpha", Broken()
            )

    def test_scope_is_cached_then_cleared(self, users_table):
        _seed_user(users_table, "ann@example.com", allowed=["ts-alpha"])
        assert get_allowed_test_sets("ann@example.com", users_table) == ["ts-alpha"]

        # Change the record; the cached value still answers.
        _seed_user(users_table, "ann@example.com", allowed=["ts-beta"])
        assert get_allowed_test_sets("ann@example.com", users_table) == ["ts-alpha"]

        testset_scope.clear_scope_cache()
        assert get_allowed_test_sets("ann@example.com", users_table) == ["ts-beta"]

    def test_missing_email_returns_no_scope(self, users_table):
        assert get_allowed_test_sets("", users_table) is None


@pytest.mark.unit
class TestScopeCacheAsymmetry:
    """Being slow to revoke is a deliberate trade; being slow to grant is a bug.

    The cache exists because scope is read on every queue read and every review
    operation, and its TTL bounds how long a REVOKED annotator keeps access. Caching
    a *denial* for the same duration has no such justification: it leaves a
    newly-assigned annotator locked out for the full TTL.
    """

    def test_an_empty_scope_is_cached_far_more_briefly_than_a_real_one(self):
        assert (
            testset_scope._EMPTY_SCOPE_CACHE_TTL_SECONDS
            < testset_scope._SCOPE_CACHE_TTL_SECONDS
        )

    def test_a_denial_does_not_outlive_the_grant_that_follows_it(self, monkeypatch):
        """A newly granted scope must be visible almost immediately."""
        testset_scope.clear_scope_cache()
        table = MagicMock()
        # First lookup: no assignment yet.
        table.query.return_value = {"Items": [{"allowedTestSets": []}]}
        assert testset_scope.get_allowed_test_sets("a@example.com", table) is None

        # Access is granted moments later.
        table.query.return_value = {"Items": [{"allowedTestSets": ["set-a"]}]}

        # Just past the short empty-scope TTL, the grant is picked up.
        now = time.time()
        monkeypatch.setattr(
            testset_scope.time,
            "time",
            lambda: now + testset_scope._EMPTY_SCOPE_CACHE_TTL_SECONDS + 1,
        )
        assert testset_scope.get_allowed_test_sets("a@example.com", table) == ["set-a"]

    def test_a_real_scope_still_uses_the_full_ttl(self, monkeypatch):
        """Read cost is the reason the cache exists; don't undo it for grants."""
        testset_scope.clear_scope_cache()
        table = MagicMock()
        table.query.return_value = {"Items": [{"allowedTestSets": ["set-a"]}]}
        assert testset_scope.get_allowed_test_sets("b@example.com", table) == ["set-a"]
        calls_after_first = table.query.call_count

        now = time.time()
        monkeypatch.setattr(
            testset_scope.time,
            "time",
            lambda: now + testset_scope._EMPTY_SCOPE_CACHE_TTL_SECONDS + 1,
        )
        # Still cached — a populated scope is not re-read on the short TTL.
        assert testset_scope.get_allowed_test_sets("b@example.com", table) == ["set-a"]
        assert table.query.call_count == calls_after_first
