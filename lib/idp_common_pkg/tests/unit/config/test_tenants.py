# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Tenant declarations: the boundary checks that keep prefix mappings, user scopes and
ingest inside one tenant.

Table-driven, for the reason test_prefix_mappings.py is: every check is a pure
function, and each one's failure is silent — a mapping, scope or document that
crosses a tenant boundary looks exactly like one that does not.
"""

import json
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from idp_common.config import tenants as t
from idp_common.config_scope import (
    STRICT_UNSCOPED_DENY,
    TENANT_ISOLATION_ENV,
    resolve_allowed_config_versions,
    scope_allows,
)

ACME = {"tenantId": "acme", "prefixes": ["clients/acme/"], "profilePattern": "acme-*"}
GLOBEX = {
    "tenantId": "globex",
    "prefixes": ["clients/globex/", "feeds/globex/"],
    "profilePattern": "globex_*",
}
TENANTS = [ACME, GLOBEX]


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry,fragment",
    [
        ({**ACME, "tenantId": "Acme"}, "lowercase"),
        ({**ACME, "tenantId": ""}, "lowercase"),
        ({**ACME, "prefixes": []}, "at least one"),
        ({**ACME, "prefixes": ["clients/acme"]}, "must end in '/'"),
        ({**ACME, "prefixes": ["/clients/acme/"]}, "must not start with '/'"),
        ({**ACME, "prefixes": ["a/", "a/b/"]}, "overlap"),
        ({**ACME, "prefixes": [f"p{i}/" for i in range(6)]}, "at most"),
        ({**ACME, "profilePattern": "acme*"}, "separator"),
        ({**ACME, "profilePattern": "acme-"}, "stem"),
        ({**ACME, "profilePattern": "ac*me-*"}, "stem"),
        ({**ACME, "profilePattern": "acme-x"}, "stem"),
    ],
)
def test_malformed_declarations_are_refused(entry, fragment):
    reason = t.tenant_rejection_reason(entry, [])
    assert reason and fragment in reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry,fragment",
    [
        # Prefixes: equal, inside, containing.
        ({**ACME, "tenantId": "new", "profilePattern": "new-*"}, "overlaps tenant"),
        (
            {
                "tenantId": "new",
                "prefixes": ["clients/acme/eu/"],
                "profilePattern": "new-*",
            },
            "overlaps tenant",
        ),
        (
            {"tenantId": "new", "prefixes": ["clients/"], "profilePattern": "new-*"},
            "overlaps tenant",
        ),
        # Stems: equal, one beginning the other in either direction.
        (
            {"tenantId": "new", "prefixes": ["n/"], "profilePattern": "acme-*"},
            "pattern",
        ),
        (
            {"tenantId": "new", "prefixes": ["n/"], "profilePattern": "acme-eu-*"},
            "pattern",
        ),
    ],
)
def test_declarations_must_be_disjoint_from_the_others(entry, fragment):
    reason = t.tenant_rejection_reason(entry, TENANTS)
    assert reason and fragment in reason


@pytest.mark.unit
def test_an_edit_is_not_compared_with_its_own_previous_version():
    assert (
        t.tenant_rejection_reason(
            {**ACME, "prefixes": ["clients/acme/", "x/"]}, TENANTS
        )
        is None
    )


@pytest.mark.unit
def test_a_sibling_prefix_sharing_characters_is_not_an_overlap():
    # `clients/acme/` and `clients/acmecorp/` share a string prefix but not a
    # folder: the trailing slash is the boundary.
    other = {
        "tenantId": "corp",
        "prefixes": ["clients/acmecorp/"],
        "profilePattern": "corp-*",
    }
    assert t.tenant_rejection_reason(other, TENANTS) is None


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "key,owner",
    [
        ("clients/acme/x.pdf", "acme"),
        ("clients/acme/deep/er/x.pdf", "acme"),
        # Canonicalized first: each of these is a distinct S3 key that would
        # otherwise be a one-character route out of the tenant.
        ("/clients/acme/x.pdf", "acme"),
        ("clients//acme/x.pdf", "acme"),
        ("clients/./acme/x.pdf", "acme"),
        ("feeds/globex/x.pdf", "globex"),
        ("clients/acmecorp/x.pdf", None),
        ("clients/x.pdf", None),
        ("", None),
    ],
)
def test_tenant_for_key(key, owner):
    found = t.tenant_for_key(key, TENANTS)
    assert (found["tenantId"] if found else None) == owner


@pytest.mark.unit
@pytest.mark.parametrize(
    "profile,owner",
    [
        ("acme-invoices", "acme"),
        ("acme-", "acme"),
        ("globex_w2", "globex"),
        ("acmecorp-x", None),
        ("globex-w2", None),
        ("", None),
        (None, None),
    ],
)
def test_tenant_for_profile(profile, owner):
    found = t.tenant_for_profile(profile, TENANTS)
    assert (found["tenantId"] if found else None) == owner


# ---------------------------------------------------------------------------
# Prefix mappings
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "prefix,profile,allowed",
    [
        ("clients/acme/", "acme-invoices", True),
        ("clients/acme/eu/", "acme-eu", True),
        ("clients/acme/one.pdf", "acme-x", True),
        # Inside a tenant, naming another tenant's profile or an untenanted one.
        ("clients/acme/eu/", "globex_w2", False),
        ("clients/acme/", "lending", False),
        # Outside every tenant, naming a tenant's profile.
        ("other/", "acme-invoices", False),
        # Outside every tenant, untenanted profile: none of this module's business.
        ("other/", "lending", True),
        # An ancestor of a tenant subtree is outside every tenant.
        ("clients/", "lending", True),
        ("clients/", "acme-x", False),
    ],
)
def test_mapping_rejection(prefix, profile, allowed):
    reason = t.mapping_rejection_reason(prefix, profile, TENANTS)
    assert (reason is None) == allowed, reason


@pytest.mark.unit
def test_declaring_a_tenant_over_a_crossing_mapping_is_refused():
    mappings = [{"prefix": "clients/acme/eu/", "configProfile": "globex_w2"}]
    reason = t.mappings_rejection_reason(mappings, TENANTS)
    assert reason and "clients/acme/eu/" in reason
    assert t.mappings_rejection_reason(mappings, []) is None


# ---------------------------------------------------------------------------
# User scope
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "scope,allowed",
    [
        (None, True),
        ([], True),
        (["acme-*"], True),
        (["acme-invoices", "acme-eu-*", "acme-?"], True),
        (["lending", "rvl-*"], True),
        (["acme-*", "globex_*"], False),
        (["acme-x", "lending"], False),
        # Outside every tenant but able to match a tenant's profile.
        (["a*"], False),
        (["acme*"], False),
        (["*"], False),
        (["[ag]*"], False),
    ],
)
def test_scope_rejection(scope, allowed):
    reason = t.scope_rejection_reason(scope, TENANTS)
    assert (reason is None) == allowed, reason


@pytest.mark.unit
def test_scope_is_unchecked_with_no_tenants_declared():
    assert t.scope_rejection_reason(["acme-*", "globex_*"], []) is None


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "key,profile,source,allowed",
    [
        ("clients/acme/x.pdf", "acme-invoices", None, True),
        ("clients/acme/x.pdf", "globex_w2", None, False),
        ("clients/acme/x.pdf", "lending", None, False),
        ("other/x.pdf", "acme-invoices", None, False),
        # Outside every tenant: refused for an external upload ...
        ("other/x.pdf", "lending", None, False),
        # ... accepted for an internal producer, whose reach is an untenanted
        # profile nobody tenant-scoped can see.
        ("other/x.pdf", "lending", "pii-anonymizer", True),
        # A forged source does not move a document across a boundary.
        ("clients/acme/x.pdf", "globex_w2", "pii-anonymizer", False),
        ("other/x.pdf", "acme-x", "pii-anonymizer", False),
    ],
)
def test_ingest_rejection(key, profile, source, allowed):
    reason = t.ingest_rejection_reason(key, profile, TENANTS, submission_source=source)
    assert (reason is None) == allowed, reason


@pytest.mark.unit
def test_a_test_run_into_a_tenant_profile_needs_its_stored_record():
    runs = {"run-1": "acme-invoices"}
    check = lambda run: runs.get(run)  # noqa: E731

    def reason(key, profile, source="test-studio"):
        return t.ingest_rejection_reason(
            key, profile, TENANTS, submission_source=source, test_run_profile=check
        )

    assert reason("run-1/doc.pdf", "acme-invoices") is None
    # The record names a different profile, there is no record, or the source is not
    # Test Studio: refused.
    assert reason("run-1/doc.pdf", "acme-other")
    assert reason("run-2/doc.pdf", "acme-invoices")
    assert reason("run-1/doc.pdf", "acme-invoices", source="pii-anonymizer")
    # A lookup that fails is an unverified claim, not a verified one.
    assert t.ingest_rejection_reason(
        "run-1/doc.pdf",
        "acme-invoices",
        TENANTS,
        submission_source="test-studio",
        test_run_profile=lambda run: 1 / 0,
    )


@pytest.mark.unit
def test_ingest_has_no_boundary_to_check_with_no_tenants():
    assert t.ingest_rejection_reason("other/x.pdf", "lending", []) is None


# ---------------------------------------------------------------------------
# Strict-mode scope
# ---------------------------------------------------------------------------


def _users_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName="users",
        KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "userId", "AttributeType": "S"},
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
    table.put_item(Item={"userId": "USER#u1", "email": "open@example.com"})
    table.put_item(
        Item={
            "userId": "USER#u2",
            "email": "scoped@example.com",
            "allowedConfigVersions": ["acme-*"],
        }
    )
    return ddb


@pytest.mark.unit
@mock_aws
@pytest.mark.parametrize(
    "mode,expected", [(None, None), ("Off", None), ("Strict", [STRICT_UNSCOPED_DENY])]
)
def test_strict_mode_denies_an_unscoped_caller(monkeypatch, mode, expected):
    if mode is None:
        monkeypatch.delenv(TENANT_ISOLATION_ENV, raising=False)
    else:
        monkeypatch.setenv(TENANT_ISOLATION_ENV, mode)
    ddb = _users_table()
    scope = resolve_allowed_config_versions(
        "open@example.com", users_table_name="users", dynamodb=ddb
    )
    assert scope == expected
    # A scoped caller is unaffected by the mode.
    assert resolve_allowed_config_versions(
        "scoped@example.com", users_table_name="users", dynamodb=ddb
    ) == ["acme-*"]
    # And a caller with no row at all is unscoped too.
    assert (
        resolve_allowed_config_versions(
            "nobody@example.com", users_table_name="users", dynamodb=ddb
        )
        == expected
    )


@pytest.mark.unit
@mock_aws
def test_a_cached_scope_is_rejudged_against_the_current_mode(monkeypatch):
    ddb = _users_table()
    cache = {}
    monkeypatch.setenv(TENANT_ISOLATION_ENV, "Off")
    assert (
        resolve_allowed_config_versions(
            "open@example.com", users_table_name="users", dynamodb=ddb, cache=cache
        )
        is None
    )
    monkeypatch.setenv(TENANT_ISOLATION_ENV, "Strict")
    assert resolve_allowed_config_versions(
        "open@example.com", users_table_name="users", dynamodb=ddb, cache=cache
    ) == [STRICT_UNSCOPED_DENY]


@pytest.mark.unit
@pytest.mark.parametrize(
    "profile", ["acme-x", "default", "", None, "\x00no-tenant-scope*"]
)
def test_the_deny_sentinel_matches_no_profile(profile):
    assert not scope_allows([STRICT_UNSCOPED_DENY], profile)


@pytest.mark.unit
def test_the_sentinel_cannot_be_a_valid_profile_name():
    # Profile names are validated to [A-Za-z0-9._-]; the sentinel starts with NUL.
    assert not STRICT_UNSCOPED_DENY[0].isprintable()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _config_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.create_table(
        TableName="config",
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.mark.unit
@mock_aws
def test_store_round_trip_and_refusals():
    store = t.TenantStore(_config_table())
    assert store.list() == []
    store.put("acme", ["clients/acme/"], "acme-*", actor="admin@example.com")
    store.put("globex", ["clients/globex/"], "globex_*", actor="admin@example.com")
    assert [x["tenantId"] for x in store.list()] == ["acme", "globex"]
    with pytest.raises(ValueError, match="overlaps"):
        store.put("dup", ["clients/acme/eu/"], "dup-*")
    # A validate_against refusal leaves the index untouched.
    with pytest.raises(ValueError, match="nope"):
        store.put("new", ["n/"], "new-*", validate_against=lambda tenants: "nope")
    assert store.get("new") is None
    # Creation provenance survives an edit.
    edited = store.put(
        "acme", ["clients/acme/", "x/"], "acme-*", actor="other@example.com"
    )
    assert edited["createdBy"] == "admin@example.com"
    assert edited["updatedBy"] == "other@example.com"
    assert store.delete("acme") is True
    assert store.delete("acme") is False


@pytest.mark.unit
@mock_aws
def test_the_index_is_not_a_profile_key():
    assert not t.TENANT_INDEX_KEY.startswith("Config#")


@pytest.mark.unit
def test_the_bounds_hold_the_item_limit():
    # Worst case at every bound, serialized the way DynamoDB counts it closely
    # enough (UTF-8 bytes of names and values).
    entry = {
        "tenantId": "a" * 63,
        "prefixes": [("p" * 510) + f"{i}/" for i in range(t.MAX_PREFIXES_PER_TENANT)],
        "profilePattern": "s" * 47 + "-*",
        "description": "d" * 500,
        "createdAt": "2026-01-01T00:00:00.000000Z",
        "createdBy": "c" * 256,
        "updatedAt": "2026-01-01T00:00:00.000000Z",
        "updatedBy": "u" * 256,
    }
    size = len(json.dumps([entry] * t.MAX_TENANTS).encode("utf-8"))
    # The design budget, not DynamoDB's limit: the approximation omits the key,
    # the counter and DynamoDB's own attribute-name accounting.
    assert size < 300 * 1024, size


@pytest.mark.unit
def test_normalize_tolerates_dynamodb_types():
    entry = t.normalize_tenant(
        {
            "tenantId": "acme",
            "prefixes": "clients/acme/",
            "profilePattern": "acme-*",
            "x": Decimal(1),
        }
    )
    assert entry["prefixes"] == ["clients/acme/"]


# ---------------------------------------------------------------------------
# Stored user scopes, re-checked when a tenant is saved
# ---------------------------------------------------------------------------


class _PagedUsers:
    """A UsersTable double returning its rows over two Scan pages."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        index = 0 if "ExclusiveStartKey" not in kwargs else kwargs["ExclusiveStartKey"]
        page = {"Items": self.pages[index]}
        if index + 1 < len(self.pages):
            page["LastEvaluatedKey"] = index + 1
        return page


def _row(pk, email, scope, persona="Viewer"):
    return {
        "PK": pk,
        "email": email,
        "persona": persona,
        "allowedConfigVersions": scope,
    }


@pytest.mark.unit
def test_stored_scopes_reads_every_page_and_only_user_rows():
    table = _PagedUsers(
        [
            [_row("USER#1", "a@x", ["acme-*"])],
            [
                _row("USER#2", "b@x", ["globex_*"]),
                _row("SUB#s", "", ["acme-*"]),  # a pointer, never a user's scope
                _row("USER#3", "admin@x", ["acme-*", "globex_*"], persona="Admin"),
                _row("USER#4", "blank@x", [""]),
            ],
        ]
    )
    rows = t.stored_scopes(table)
    assert [r["email"] for r in rows] == ["a@x", "b@x"]
    assert len(table.calls) == 2, "the second page must be read"
    assert table.calls[0]["ProjectionExpression"].count(",") == 3


@pytest.mark.unit
def test_stored_scopes_rejection_names_one_user_and_counts_the_rest():
    scopes = [
        {"email": "ok@x", "allowedConfigVersions": ["acme-*"]},
        {"email": "wide@x", "allowedConfigVersions": ["acme-*", "globex_*"]},
        {"email": "wide2@x", "allowedConfigVersions": ["a*"]},
    ]
    reason = t.stored_scopes_rejection_reason(scopes, TENANTS)
    assert reason and "wide@x" in reason and "1 other user(s)" in reason
    assert t.stored_scopes_rejection_reason(scopes[:1], TENANTS) is None
    assert t.stored_scopes_rejection_reason(scopes, []) is None
