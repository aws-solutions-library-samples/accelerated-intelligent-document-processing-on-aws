# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Tenant declaration operations in the configuration resolver, and the tenant check
on prefix-mapping writes.

The properties under test:

- **CRUD is Admin-only, server-side**, on the same grounds as mapping CRUD.
- **A mapping that crosses a tenant boundary is refused**, in both directions: a
  prefix inside a tenant naming a profile that is not that tenant's, and a prefix
  outside every tenant naming a tenant's profile.
- **A tenant declared over an existing crossing mapping is refused**, and the
  index is left as it was.
- **An unreadable tenant index refuses a mapping write** rather than skipping the
  check.

Uses a real (moto) ConfigurationTable for both stores, because the interaction
under test is between two items in that one table.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "test-config-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "config_resolver_index_tenants", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["config_resolver_index_tenants"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()

ACME = {
    "tenantId": "acme",
    "prefixes": ["clients/acme/"],
    "profilePattern": "acme-*",
}
GLOBEX = {
    "tenantId": "globex",
    "prefixes": ["clients/globex/"],
    "profilePattern": "globex-*",
}


def _event(field, args=None, groups=("Admin",), email="admin@example.com"):
    return {
        "info": {"fieldName": field},
        "arguments": args or {},
        "identity": {"claims": {"cognito:groups": list(groups), "email": email}},
    }


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = ddb.create_table(
            TableName="test-config-table",
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        for profile in ("acme-invoices", "globex-w2", "lending"):
            t.put_item(Item={"Configuration": f"Config#{profile}"})
        # The host UsersTable, keyed as the template declares it, so saving a
        # tenant can read the stored scopes.
        ddb.create_table(
            TableName=USERS_TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t


USERS_TABLE = "test-users-table"


def _put_user(email, scope=None, user_id=None, persona="Viewer"):
    item = {"PK": f"USER#{user_id or email}", "SK": f"USER#{user_id or email}"}
    item["userId"] = user_id or email
    item["email"] = email
    item["persona"] = persona
    if scope is not None:
        item["allowedConfigVersions"] = scope
    boto3.resource("dynamodb", region_name="us-east-1").Table(USERS_TABLE).put_item(
        Item=item
    )


@pytest.fixture
def manager(monkeypatch, table):
    from idp_common.config.configuration_manager import ConfigurationManager

    monkeypatch.setenv("USERS_TABLE_NAME", USERS_TABLE)
    # The module's resource was built at import, outside this test's mock; give it
    # one built inside it so the UsersTable read lands on the table created above.
    monkeypatch.setattr(
        index, "_dynamodb", boto3.resource("dynamodb", region_name="us-east-1")
    )
    fake = MagicMock(spec=ConfigurationManager)
    fake.table = table
    fake.get_revision.return_value = {"notes": "a revision body"}
    fake.mark_revision_pinned.return_value = True
    monkeypatch.setattr(index, "ConfigurationManager", lambda *a, **k: fake)
    monkeypatch.setattr(
        index, "_get_user_allowed_config_versions", lambda email, sub="": None
    )
    return fake


def _put_tenant(entry):
    return index.handler(_event("putTenant", dict(entry)), None)


def _put_mapping(prefix, profile):
    return index.handler(
        _event("putConfigPrefixMapping", {"prefix": prefix, "configProfile": profile}),
        None,
    )


# ---------------------------------------------------------------------------
# The group gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "field,args",
    [
        ("listTenants", {}),
        ("putTenant", ACME),
        ("deleteTenant", {"tenantId": "acme"}),
    ],
)
@pytest.mark.parametrize("group", ["Author", "Viewer", "Reviewer", "Annotator"])
def test_tenant_crud_is_admin_only(manager, field, args, group):
    with pytest.raises(Exception, match="Unauthorized"):
        index.handler(_event(field, dict(args), groups=(group,)), None)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("mode,expected", [("Strict", "Strict"), ("Off", "Off")])
def test_list_reports_the_tenants_and_the_isolation_mode(
    manager, monkeypatch, mode, expected
):
    monkeypatch.setenv("TENANT_ISOLATION_MODE", mode)
    assert _put_tenant(ACME)["success"] is True
    result = index.handler(_event("listTenants"), None)
    assert result["success"] is True
    assert result["isolationMode"] == expected
    assert [t["tenantId"] for t in result["tenants"]] == ["acme"]
    assert result["tenants"][0]["createdBy"] == "admin@example.com"


@pytest.mark.unit
def test_put_refuses_an_overlapping_tenant(manager):
    assert _put_tenant(ACME)["success"] is True
    result = _put_tenant(
        {"tenantId": "dup", "prefixes": ["clients/acme/eu/"], "profilePattern": "dup-*"}
    )
    assert result["success"] is False
    assert result["error"]["type"] == "ValidationError"
    assert "overlaps" in result["error"]["message"]


@pytest.mark.unit
def test_put_refuses_a_tenant_declared_over_a_crossing_mapping(manager):
    # Before the tenant exists, mapping a folder to globex's profile is allowed.
    assert _put_mapping("clients/acme/eu/", "globex-w2")["success"] is True
    result = _put_tenant(ACME)
    assert result["success"] is False
    assert result["error"]["type"] == "ValidationError"
    assert "clients/acme/eu/" in result["error"]["message"]
    # The index is untouched.
    assert index.handler(_event("listTenants"), None)["tenants"] == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "scope",
    [
        ["acme-invoices", "globex-w2"],  # would span both tenants
        ["acme-invoices", "lending"],  # a tenant plus an untenanted profile
        ["a*"],  # a glob that reaches into acme without naming its stem
    ],
)
def test_put_refuses_a_tenant_set_that_leaves_a_stored_scope_crossing(manager, scope):
    """A scope is checked when it is written, and nothing re-checks it after, so
    declaring a tenant over an existing user's scope must be refused here."""
    assert _put_tenant(GLOBEX)["success"] is True
    _put_user("someone@example.com", scope)
    result = _put_tenant(ACME)
    assert result["success"] is False
    assert result["error"]["type"] == "ValidationError"
    assert "someone@example.com" in result["error"]["message"]
    tenants = index.handler(_event("listTenants"), None)["tenants"]
    assert [t["tenantId"] for t in tenants] == ["globex"]


@pytest.mark.unit
def test_put_accepts_stored_scopes_inside_one_tenant(manager):
    _put_user("acme@example.com", ["acme-*"])
    _put_user("open@example.com")  # no scope at all: nothing to check
    _put_user("other@example.com", ["lending"])  # untenanted only
    # An Admin's stored scope is never applied, so it cannot block the save.
    _put_user("admin@example.com", ["acme-x", "lending"], persona="Admin")
    assert _put_tenant(ACME)["success"] is True


@pytest.mark.unit
def test_put_counts_every_offending_user(manager):
    assert _put_tenant(GLOBEX)["success"] is True
    for n in range(3):
        _put_user(f"u{n}@example.com", ["acme-x", "globex-w2"])
    message = _put_tenant(ACME)["error"]["message"]
    assert "2 other user(s)" in message


@pytest.mark.unit
def test_put_refuses_when_stored_scopes_cannot_be_read(manager, monkeypatch):
    monkeypatch.setenv("USERS_TABLE_NAME", "")
    result = _put_tenant(ACME)
    assert result["success"] is False
    assert index.handler(_event("listTenants"), None)["tenants"] == []


@pytest.mark.unit
def test_delete_reports_missing_and_removes(manager):
    missing = index.handler(_event("deleteTenant", {"tenantId": "acme"}), None)
    assert missing["success"] is False
    assert missing["error"]["type"] == "NotFound"
    _put_tenant(ACME)
    assert index.handler(_event("deleteTenant", {"tenantId": "acme"}), None)["success"]
    assert index.handler(_event("listTenants"), None)["tenants"] == []


# ---------------------------------------------------------------------------
# Prefix mappings across a tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "prefix,profile,allowed",
    [
        ("clients/acme/", "acme-invoices", True),
        ("clients/acme/eu/", "acme-invoices", True),
        # Inside acme, naming another tenant's profile or an untenanted one.
        ("clients/acme/eu/", "globex-w2", False),
        ("clients/acme/", "lending", False),
        # Outside every tenant, naming a tenant's profile.
        ("other/", "acme-invoices", False),
        ("other/", "lending", True),
    ],
)
def test_mapping_writes_respect_tenant_boundaries(manager, prefix, profile, allowed):
    _put_tenant(ACME)
    _put_tenant(GLOBEX)
    result = _put_mapping(prefix, profile)
    assert result["success"] is allowed, result
    if not allowed:
        assert result["error"]["type"] == "ValidationError"
        assert "tenant" in result["error"]["message"]


@pytest.mark.unit
def test_an_unreadable_tenant_index_refuses_the_mapping_write(manager, monkeypatch):
    broken = MagicMock()
    broken.list.side_effect = RuntimeError("throttled")
    monkeypatch.setattr(index, "_tenant_store", lambda _m: broken)
    result = _put_mapping("other/", "lending")
    assert result["success"] is False
    # Nothing was written.
    assert index.handler(_event("listConfigPrefixMappings"), None)["mappings"] == []


# ---------------------------------------------------------------------------
# The upload dry run
# ---------------------------------------------------------------------------


def _dry_run(key):
    return index.handler(_event("resolveConfigPrefixMapping", {"objectKey": key}), None)


@pytest.mark.unit
@pytest.mark.parametrize(
    "mode,key,rejected",
    [
        # No mapping covers the key, so the active profile (`lending`, no tenant)
        # applies: a tenant key under an untenanted profile.
        ("Strict", "clients/acme/x.pdf", True),
        # The tenant's own mapping applies.
        ("Strict", "clients/acme/mapped/x.pdf", False),
        # Outside every tenant, external upload.
        ("Strict", "elsewhere/x.pdf", True),
        # Off mode reports what ingest will do there, which is to process it.
        ("Off", "clients/acme/x.pdf", False),
    ],
)
def test_the_dry_run_reports_a_strict_tenant_refusal(
    manager, monkeypatch, key, mode, rejected
):
    manager.resolve_active_version.return_value = "lending"
    manager.resolve_published_revision.return_value = None
    assert _put_tenant(ACME)["success"] is True
    assert _put_tenant(GLOBEX)["success"] is True
    assert _put_mapping("clients/acme/mapped/", "acme-invoices")["success"] is True
    monkeypatch.setenv("TENANT_ISOLATION_MODE", mode)
    assignment = _dry_run(key)["assignment"]
    assert bool(assignment["rejected"]) is rejected
    if rejected:
        # Generic: it must not name the tenant that owns the key.
        assert "acme" not in assignment["reason"]
        assert "strict tenant isolation" in assignment["reason"]
