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

The lookup reads **two** key spaces, and ``TestTheSubJoin`` and
``TestTheTransition`` below are what hold the second one honest: the immutable
Cognito ``sub``, via a ``SUB#<sub>`` pointer item, and the ``email`` claim, via
``EmailIndex``. Preferring the ``sub`` is what keeps a restriction working when an
address has diverged from the row it should match — the divergence otherwise reads
as "no row", which means unrestricted. Neither identifier is ever substituted for
the other, and the transition (some rows carry a ``sub``, some do not) must resolve
every row shape without locking anyone out.
"""

from unittest.mock import MagicMock

import pytest

from idp_common.config_scope import (
    USERS_TABLE_SCOPE_INDEX,
    USERS_TABLE_SCOPE_KEY,
    USERS_TABLE_SUB_ATTRIBUTE,
    USERS_TABLE_SUB_POINTER_PREFIX,
    USERS_TABLE_USER_KEY_PREFIX,
    ScopeLookupError,
    caller_email_from_claims,
    caller_sub_from_claims,
    resolve_allowed_config_versions,
    sub_pointer_key,
    user_row_key,
)

_SUB = "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d"


_DEFAULT_EMAIL = "a@example.com"


def _table_returning(items, store=None, email=_DEFAULT_EMAIL):
    """A UsersTable double covering BOTH key spaces the lookup reads.

    ``store`` maps a raw ``PK`` string to the item held under it, which is how the
    ``sub`` join is modelled: a ``SUB#<sub>`` pointer carrying a ``userId``, and the
    ``USER#<userId>`` row it names.

    **Both legs honour their key**, and that is the point of this double rather than a
    bare MagicMock. Left to one, ``get_item`` answers any key with a truthy Mock, so a
    test passes while the code reads a row that does not exist; and ``query`` answers
    any key condition with the same page, so a test passes while the code puts the
    *wrong identifier* to ``EmailIndex`` — which is this module's original defect.
    ``items`` is therefore the page for ``email`` and for no other address.
    """
    table = MagicMock()

    def _query(**kwargs):
        condition = kwargs["KeyConditionExpression"]
        queried = condition.get_expression()["values"][1]
        return {"Items": items if queried == email else []}

    table.query.side_effect = _query
    _store = dict(store or {})
    table.get_item.side_effect = lambda Key: (
        {"Item": _store[Key["PK"]]} if Key["PK"] in _store else {}
    )
    return table


def _resource_for(table):
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


def _pointing_at(user_id, row, sub=_SUB):
    """A ``store`` in which ``sub``'s pointer names ``user_id``, holding ``row``."""
    return {
        sub_pointer_key(sub)["PK"]: {"userId": user_id, USERS_TABLE_SUB_ATTRIBUTE: sub},
        user_row_key(user_id)["PK"]: row,
    }


@pytest.mark.unit
class TestTheKeysThemselves:
    """The literal keys, asserted against literals rather than against the helpers.

    Every other test here builds its fixture store with ``sub_pointer_key`` and
    ``user_row_key`` — the same functions the code under test uses — so fixture and
    code move together and a rename of the prefix is invisible. Renaming
    ``USERS_TABLE_SUB_POINTER_PREFIX`` to anything else and re-syncing the vendored
    copies exactly as their drift test instructs left the whole suite green, while
    every host resolver read the new prefix and ``user_management`` still wrote
    ``SUB#`` — so every caller's ``sub`` leg silently returned nothing and the lookup
    reverted to email-only.

    The writer and the pii-anonymizer handler are already pinned by accident, because
    both hardcode ``f"SUB#{sub}"``; the canonical reader, which every host resolver
    imports, was the only one unpinned. This is the exact failure mode
    ``sub_pointer_key``'s own docstring warns about.
    """

    def test_the_sub_pointer_key_is_the_literal_every_writer_writes(self):
        assert sub_pointer_key(_SUB) == {"PK": f"SUB#{_SUB}", "SK": f"SUB#{_SUB}"}

    def test_the_user_row_key_is_the_literal_user_management_writes(self):
        assert user_row_key("u-1") == {"PK": "USER#u-1", "SK": "USER#u-1"}

    def test_the_prefixes_are_distinct_and_neither_prefixes_the_other(self):
        """A pointer must not be mistakable for a row by a ``begins_with`` filter.

        ``list_users`` and the Cognito sync both scan on ``begins_with(PK, "USER#")``,
        and the UsersTable stream consumer's ``is_user_record`` tests the same prefix.
        A pointer prefix that started with the row prefix would put pointer items into
        all three.
        """
        assert not USERS_TABLE_SUB_POINTER_PREFIX.startswith(
            USERS_TABLE_USER_KEY_PREFIX
        )
        assert not USERS_TABLE_USER_KEY_PREFIX.startswith(
            USERS_TABLE_SUB_POINTER_PREFIX
        )

    def test_the_email_index_and_its_key_are_the_declared_names(self):
        assert USERS_TABLE_SCOPE_INDEX == "EmailIndex"
        assert USERS_TABLE_SCOPE_KEY == "email"
        assert USERS_TABLE_SUB_ATTRIBUTE == "cognitoSub"


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

    def test_the_email_query_is_keyed_on_the_callers_own_address(self):
        """The *value* put to EmailIndex, not just the index and attribute names.

        A double that answers any key condition with the same page passes while the
        code puts the wrong identifier to an email-keyed index — which is this
        module's original defect, and exactly what the two assertions above cannot
        see: they read the index name and the attribute the condition names, never
        the value compared against it.
        """
        table = _table_returning(
            [{"allowedConfigVersions": ["lending"]}], email="owner@example.com"
        )
        resource = _resource_for(table)

        assert resolve_allowed_config_versions(
            "owner@example.com", users_table_name="UsersTable", dynamodb=resource
        ) == ["lending"]
        assert (
            resolve_allowed_config_versions(
                "someone.else@example.com",
                users_table_name="UsersTable",
                dynamodb=resource,
            )
            is None
        )

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

    def test_neither_identifier_denies_and_issues_no_read(self):
        """An unresolvable caller must not be promoted to an unrestricted one."""
        table = _table_returning([])

        with pytest.raises(ScopeLookupError):
            resolve_allowed_config_versions(
                "",
                users_table_name="UsersTable",
                dynamodb=_resource_for(table),
            )

        table.query.assert_not_called()
        table.get_item.assert_not_called()

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
        """Populated by a real call, so the test cannot miss by guessing the key.

        A hand-built entry under the wrong key would make this pass by *missing*
        the cache rather than by expiring it, which proves nothing about the TTL.
        """
        cache = {}
        table = _table_returning([{"allowedConfigVersions": ["stale"]}])
        resource = _resource_for(table)
        assert resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=resource,
            cache=cache,
        ) == ["stale"]
        assert len(cache) == 1
        for entry in cache.values():
            entry["timestamp"] = 0.0
        table.query.side_effect = lambda **kw: {
            "Items": [{"allowedConfigVersions": ["fresh"]}]
        }

        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=resource,
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

    def test_the_cache_is_not_keyed_across_the_two_identifiers(self):
        """One address, two Cognito accounts: one entry must not serve both.

        A cache keyed on the email alone would hand a caller resolved by ``sub``
        the scope of a different caller who happened to share an address, and a
        cache keyed on the ``sub`` alone would do the reverse for a caller carrying
        no ``sub``.
        """
        cache = {}
        table = _table_returning(
            [{"allowedConfigVersions": ["by-email"]}],
            store=_pointing_at("u-1", {"allowedConfigVersions": ["by-sub"]}),
            email="shared@example.com",
        )
        resource = _resource_for(table)

        by_sub = resolve_allowed_config_versions(
            "shared@example.com",
            users_table_name="UsersTable",
            dynamodb=resource,
            caller_sub=_SUB,
            cache=cache,
        )
        by_email = resolve_allowed_config_versions(
            "shared@example.com",
            users_table_name="UsersTable",
            dynamodb=resource,
            cache=cache,
        )

        assert by_sub == ["by-sub"]
        assert by_email == ["by-email"]


@pytest.mark.unit
class TestCallerSubFromClaims:
    def test_reads_the_sub_claim(self):
        assert caller_sub_from_claims({"sub": _SUB}) == _SUB

    def test_surrounding_whitespace_is_trimmed(self):
        assert caller_sub_from_claims({"sub": f" {_SUB} "}) == _SUB

    @pytest.mark.parametrize(
        "claims",
        [
            {},
            None,
            "not-a-mapping",
            {"sub": ""},
            {"sub": None},
            # A body-supplied `callerSub` is not a verified claim and must not be
            # picked up as one: the caller it would restrict chooses the value.
            {"callerSub": _SUB, "cognito:username": "someone"},
        ],
    )
    def test_anything_but_the_sub_claim_yields_nothing(self, claims):
        assert caller_sub_from_claims(claims) == ""


@pytest.mark.unit
class TestTheSubJoin:
    """The immutable key space, which is why email divergence stops mattering."""

    def test_the_sub_resolves_the_scope_without_touching_the_email_index(self):
        table = _table_returning(
            [], store=_pointing_at("u-1", {"allowedConfigVersions": ["tenant-a"]})
        )

        scope = resolve_allowed_config_versions(
            "",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
            caller_sub=_SUB,
        )

        assert scope == ["tenant-a"]
        table.query.assert_not_called()

    def test_a_diverged_email_still_resolves_through_the_sub(self):
        """The residual this join closes.

        The caller's address no longer matches the one on their row — remapped by
        an IdP, changed by the user, or differing only in case. The email query
        therefore finds nothing, which on its own means *unrestricted*. The pointer
        finds the row anyway, so the restriction still applies.
        """
        table = _table_returning(
            [],  # the email query matches nothing, as in production
            store=_pointing_at("u-1", {"allowedConfigVersions": ["tenant-a"]}),
        )

        scope = resolve_allowed_config_versions(
            "Renamed.User@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
            caller_sub=_SUB,
        )

        assert scope == ["tenant-a"]

    def test_the_sub_key_space_is_read_with_the_declared_key_shape(self):
        """A pointer the writer and the reader spell differently is a silent miss."""
        table = _table_returning([], store={})

        with pytest.raises(ScopeLookupError):
            resolve_allowed_config_versions(
                "",
                users_table_name="UsersTable",
                dynamodb=_resource_for(table),
                caller_sub=_SUB,
            )

        assert table.get_item.call_args_list[0].kwargs["Key"] == sub_pointer_key(_SUB)

    @pytest.mark.parametrize(
        "error",
        [
            Exception("AccessDeniedException: not authorized: dynamodb:GetItem"),
            Exception("ProvisionedThroughputExceededException"),
            RuntimeError("something nobody anticipated"),
        ],
    )
    def test_a_pointer_read_failure_denies(self, error):
        """A failure on the new key space must not fall through to the old one.

        Falling through would make an unreadable pointer indistinguishable from an
        absent one — and for a caller whose email has diverged, that lands on an
        empty page, which means unrestricted.
        """
        table = _table_returning([{"allowedConfigVersions": ["tenant-a"]}])
        table.get_item.side_effect = error

        with pytest.raises(ScopeLookupError):
            resolve_allowed_config_versions(
                "a@example.com",
                users_table_name="UsersTable",
                dynamodb=_resource_for(table),
                caller_sub=_SUB,
            )

    def test_a_sub_only_caller_with_no_pointer_denies(self):
        """Not an answer about this caller, so not "unrestricted".

        Their row may exist and simply predate the pointer writer, and with no
        ``email`` claim there is no second key to try. An empty *email* page is an
        answer — this is a failure to get one.
        """
        with pytest.raises(ScopeLookupError):
            resolve_allowed_config_versions(
                "",
                users_table_name="UsersTable",
                dynamodb=_resource_for(_table_returning([], store={})),
                caller_sub=_SUB,
            )

    def test_a_pointer_naming_a_row_that_does_not_exist_falls_back_to_email(self):
        """A user deleted without their pointer cleaned up, then re-created."""
        table = _table_returning(
            [{"allowedConfigVersions": ["tenant-b"]}],
            store={sub_pointer_key(_SUB)["PK"]: {"userId": "u-gone"}},
        )

        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
            caller_sub=_SUB,
        )

        assert scope == ["tenant-b"]

    def test_the_sub_leg_cannot_return_an_unrestricted_row(self, caplog):
        """The property that stops this leg ever *widening* a scope.

        The writer's invariant is that a pointer exists only for a row carrying a
        restriction, because this leg is read first and therefore decides the answer.
        A pointer resolving an unscoped row means the invariant is broken, so it is
        treated as stale and the email join is tried — which can only tighten.
        Believing it instead is the one shape that would let the preferred leg answer
        "unrestricted" over a row the email join would have restricted.
        """
        table = _table_returning(
            [{"allowedConfigVersions": ["tenant-b"]}],
            store=_pointing_at("u-unscoped", {"userId": "u-unscoped"}),
        )

        with caplog.at_level("WARNING"):
            scope = resolve_allowed_config_versions(
                "a@example.com",
                users_table_name="UsersTable",
                dynamodb=_resource_for(table),
                caller_sub=_SUB,
            )

        assert scope == ["tenant-b"]
        assert any("stale" in r.getMessage() for r in caplog.records), caplog.text

    def test_a_pointer_with_no_user_id_falls_back_to_email(self):
        table = _table_returning(
            [{"allowedConfigVersions": ["tenant-b"]}],
            store={sub_pointer_key(_SUB)["PK"]: {"updatedAt": "2026-01-01T00:00:00Z"}},
        )

        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
            caller_sub=_SUB,
        )

        assert scope == ["tenant-b"]


@pytest.mark.unit
class TestTheTransition:
    """Every row shape a deployment can hold while the back-fill is outstanding.

    An upgrade starts with no pointer items at all, so the email join has to keep
    resolving every existing row unchanged. A row that silently stops being found
    is the same defect in a new costume; a caller newly locked out is worse.
    """

    def test_a_row_with_no_sub_recorded_is_still_found_by_email(self):
        table = _table_returning(
            [{"allowedConfigVersions": ["tenant-a"]}],
            store={},  # no pointers exist yet, as on every upgraded deployment
        )

        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
            caller_sub=_SUB,
        )

        assert scope == ["tenant-a"]
        table.query.assert_called_once()

    def test_a_row_with_a_sub_recorded_is_found_by_the_sub(self):
        table = _table_returning(
            [{"allowedConfigVersions": ["stale-by-email"]}],
            store=_pointing_at("u-1", {"allowedConfigVersions": ["tenant-a"]}),
        )

        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
            caller_sub=_SUB,
        )

        assert scope == ["tenant-a"]

    def test_a_row_whose_recorded_sub_contradicts_the_caller_still_applies(
        self, caplog
    ):
        """The address's row is used, and the mismatch is reported.

        A row found by email that records a *different* ``sub`` means one of two
        accounts has a stale row — an address reassigned, or a Cognito account
        recreated under the same one. Its scope is applied anyway, because a scope
        is a restriction and applying it is the conservative direction; ignoring it
        would land on "no row", which means unrestricted. The warning is how an
        operator learns the row needs reconciling.
        """
        table = _table_returning(
            [
                {
                    "allowedConfigVersions": ["tenant-a"],
                    USERS_TABLE_SUB_ATTRIBUTE: "a-different-account",
                }
            ],
            store={},
        )

        with caplog.at_level("WARNING"):
            scope = resolve_allowed_config_versions(
                "a@example.com",
                users_table_name="UsersTable",
                dynamodb=_resource_for(table),
                caller_sub=_SUB,
            )

        assert scope == ["tenant-a"]
        assert any(
            USERS_TABLE_SUB_ATTRIBUTE in record.getMessage()
            for record in caplog.records
        ), caplog.text

    def test_a_caller_with_no_sub_claim_behaves_exactly_as_before(self):
        """The transport may verify no ``sub``; the email join then carries it all."""
        table = _table_returning([{"allowedConfigVersions": ["tenant-a"]}], store={})

        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(table),
        )

        assert scope == ["tenant-a"]
        table.get_item.assert_not_called()

    def test_a_caller_with_neither_key_matching_anything_is_unrestricted(self):
        """The opt-in default, unchanged: no row for this user means no restriction."""
        scope = resolve_allowed_config_versions(
            "a@example.com",
            users_table_name="UsersTable",
            dynamodb=_resource_for(_table_returning([], store={})),
            caller_sub=_SUB,
        )

        assert scope is None
