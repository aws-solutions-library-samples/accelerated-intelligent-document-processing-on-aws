# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The fail-closed UsersTable scope lookup (``allowedConfigVersions``).

``test_config_scope.py`` covers the *matcher* — given a scope, which profile
names it permits. This file covers the other half: **whose** scope is being
matched. Matching the right scope against the wrong caller is not a weaker
control, it is no control at all.

Two ways that happened across eight near-copies of this lookup, both closed here
and both asserted below:

* the lookup key was derived from an ``or``-chain of claims, so a claims set
  carrying no ``email`` resolved to some other identifier, matched no row, and
  the empty page read as "this user has no restriction". No AWS fault is needed
  for that — only claims that differ from the ones the code was tested against.
* a failed DynamoDB query was caught and turned into ``None``, which is the same
  "no restriction" answer, reached from a missing IAM grant or a throttle.

An empty page **is** still unrestricted, deliberately: scoping is opt-in per user
and denying there would lock every ordinary user out of the UI. The distinction
these tests pin is between an *answer* from the table and a *failure to get one*.
"""

from unittest.mock import MagicMock

import pytest

from idp_common.config_scope import (
    USERS_TABLE_SCOPE_INDEX,
    USERS_TABLE_SCOPE_KEY,
    ScopeLookupError,
    caller_email_from_claims,
    resolve_allowed_config_versions,
)


def _table_returning(items):
    """A DynamoDB Table double that answers one Query with ``items``."""
    table = MagicMock()
    table.query.return_value = {"Items": items}
    return table


def _resource_for(table):
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


@pytest.mark.unit
class TestCallerEmailFromClaims:
    def test_reads_the_email_claim(self):
        assert caller_email_from_claims({"email": "a@example.com"}) == "a@example.com"

    def test_surrounding_whitespace_is_trimmed(self):
        assert caller_email_from_claims({"email": " a@example.com "}) == "a@example.com"

    @pytest.mark.parametrize(
        "claims",
        [
            {},
            None,
            "not-a-mapping",
            {"email": ""},
            {"email": None},
            # Every Cognito identifier EXCEPT an email. The old fallback chain
            # resolved this shape to the bare `sub`, which matches no row.
            {
                "sub": "11111111-2222-3333-4444-555555555555",
                "cognito:username": "someone",
                "username": "someone",
                "cognito:groups": ["Author"],
            },
        ],
    )
    def test_anything_but_an_email_claim_yields_nothing(self, claims):
        """No substitute identifier is ever accepted as the lookup key."""
        assert caller_email_from_claims(claims) == ""


@pytest.mark.unit
class TestResolveAllowedConfigVersions:
    def test_returns_the_scope_on_the_row(self):
        table = _table_returning([{"allowedConfigVersions": ["lending", "claims"]}])

        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
        )

        assert scope == ["lending", "claims"]

    def test_queries_the_declared_index_on_its_declared_key(self):
        """An index name is a string; a wrong one raises only at runtime."""
        table = _table_returning([])

        resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
        )

        kwargs = table.query.call_args.kwargs
        assert kwargs["IndexName"] == USERS_TABLE_SCOPE_INDEX
        condition = kwargs["KeyConditionExpression"]
        assert condition.get_expression()["values"][0].name == USERS_TABLE_SCOPE_KEY

    def test_an_empty_page_is_still_unrestricted(self):
        """Scoping is opt-in: most users have no row, and must not be denied."""
        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(_table_returning([])),
        )

        assert scope is None

    def test_a_row_with_an_empty_scope_is_unrestricted(self):
        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(_table_returning([{"allowedConfigVersions": []}])),
        )

        assert scope is None

    def test_no_caller_email_denies_and_issues_no_query(self):
        """An unresolvable caller must not be promoted to an unrestricted one."""
        table = _table_returning([])

        with pytest.raises(ScopeLookupError):
            resolve_allowed_config_versions(
                "",
                users_table_name="UsersTable",
                dynamodb=_resource_for(table),
            )

        table.query.assert_not_called()

    def test_no_table_wired_denies(self):
        """The parent template wires this unconditionally; empty means drift."""
        with pytest.raises(ScopeLookupError):
            resolve_allowed_config_versions(
                "a@example.com",
                users_table_name="",
                dynamodb=_resource_for(_table_returning([])),
            )

    @pytest.mark.parametrize(
        "error",
        [
            Exception("ValidationException: the table does not have that index"),
            Exception("AccessDeniedException: not authorized: dynamodb:Query"),
            Exception("ProvisionedThroughputExceededException"),
            RuntimeError("something nobody anticipated"),
        ],
    )
    def test_any_query_failure_denies(self, error):
        """This is THE control: it asserts the outcome, not the code shape.

        A missing IAM grant, a wrong index name, throttling and an unanticipated
        exception all have to end in a refusal. However the lookup is rewritten
        later, returning "unrestricted" from any of these fails here.
        """
        table = MagicMock()
        table.query.side_effect = error

        with pytest.raises(ScopeLookupError):
            resolve_allowed_config_versions(
                "a@example.com",
                users_table_name="UsersTable",
                dynamodb=_resource_for(table),
            )

    def test_a_failure_is_never_cached_as_an_answer(self):
        """A transient error must not pin "unrestricted" for the cache TTL."""
        cache = {}
        table = MagicMock()
        table.query.side_effect = Exception("throttled")

        with pytest.raises(ScopeLookupError):
            resolve_allowed_config_versions(
                "a@example.com",
                users_table_name="UsersTable",
                dynamodb=_resource_for(table),
                cache=cache,
            )

        assert cache == {}

    def test_a_resolved_scope_is_cached_per_caller(self):
        """Bursty UI polling must not cost one Query per request."""
        cache = {}
        table = _table_returning([{"allowedConfigVersions": ["lending"]}])
        resource = _resource_for(table)

        first = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=resource,
            cache=cache,
        )
        second = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=resource,
            cache=cache,
        )

        assert first == second == ["lending"]
        assert table.query.call_count == 1

    def test_an_expired_cache_entry_is_re_read(self):
        cache = {"a@example.com": {"scope": ["stale"], "timestamp": 0.0}}
        table = _table_returning([{"allowedConfigVersions": ["fresh"]}])

        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
            cache=cache,
            cache_ttl=1.0,
        )

        assert scope == ["fresh"]

    def test_the_cache_is_not_keyed_across_callers(self):
        """One container serves many callers; a shared entry would cross scopes."""
        cache = {}
        table = MagicMock()
        table.query.side_effect = [
            {"Items": [{"allowedConfigVersions": ["lending"]}]},
            {"Items": [{"allowedConfigVersions": ["claims"]}]},
        ]
        resource = _resource_for(table)

        a = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=resource,
            cache=cache,
        )
        b = resolve_allowed_config_versions(
            "b@example.com",
            users_table_name="UsersTable",
            dynamodb=resource,
            cache=cache,
        )

        assert a == ["lending"]
        assert b == ["claims"]
