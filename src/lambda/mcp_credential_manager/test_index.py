# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The MCP credential manager Lambda: Admin-only routing, the scoped-Admin rule, and
the scheduled sweep.

The credential operations themselves are covered against moto in
``test_mcp_credentials.py``; here they are recorders, so what is under test is
which caller reaches them and with what arguments.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

MANAGER = Path(__file__).resolve().parent / "index.py"


@pytest.fixture
def manager(monkeypatch):
    spec = importlib.util.spec_from_file_location("mcp_manager_index", MANAGER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcp_manager_index"] = module
    spec.loader.exec_module(module)
    calls = []

    def recorder(name):
        def fn(*args, **kwargs):
            calls.append((name, args[1:] if len(args) > 1 else (), kwargs))
            return {"ok": name}

        return fn

    for name in (
        "list_credentials",
        "create_credential",
        "update_credential",
        "rotate_credential",
        "revoke_credential",
        "set_credential_tenant",
        "sweep",
        "revoke_all",
    ):
        monkeypatch.setattr(module.mc, name, recorder(name))
    rows = {}
    monkeypatch.setattr(module.mc, "get_row", lambda _t, cid: rows.get(cid))
    tenants = [
        {"tenantId": "acme", "profilePattern": "acme-*"},
        {"tenantId": "globex", "profilePattern": "globex-*"},
    ]
    monkeypatch.setattr(
        module, "services", lambda: SimpleNamespace(table=None, tenants=lambda: tenants)
    )
    own = {"scope": None}
    monkeypatch.setattr(module, "admin_scope", lambda _claims: own["scope"])
    sent = []
    monkeypatch.setattr(
        module.cfnresponse,
        "send",
        lambda _e, _c, status, _data, physical, **_kw: sent.append((status, physical)),
    )
    yield SimpleNamespace(module=module, calls=calls, rows=rows, own=own, sent=sent)
    sys.modules.pop("mcp_manager_index", None)


def event(field, groups=("Admin",), **arguments):
    return {
        "info": {"fieldName": field},
        "arguments": arguments,
        "identity": {
            "claims": {"cognito:groups": list(groups), "email": "admin@example.com"}
        },
    }


@pytest.mark.parametrize(
    "field",
    [
        "listMcpCredentials",
        "createMcpCredential",
        "updateMcpCredential",
        "rotateMcpCredential",
        "revokeMcpCredential",
        "setMcpCredentialTenant",
    ],
)
@pytest.mark.parametrize("groups", [("Author",), ("Viewer",), (), ("Reviewer",)])
def test_every_operation_is_admin_only(manager, field, groups):
    with pytest.raises(PermissionError, match="Admin"):
        manager.module.handler(event(field, groups=groups, clientId="c"), None)
    assert manager.calls == []


def test_an_unknown_operation_is_refused(manager):
    with pytest.raises(ValueError, match="Unknown operation"):
        manager.module.handler(event("deleteEverything"), None)


def test_create_passes_the_request_and_the_acting_admin(manager):
    manager.module.handler(
        event(
            "createMcpCredential",
            name="nightly",
            role="Viewer",
            allowedConfigVersions=["acme-*"],
            expiresInDays=30,
        ),
        None,
    )
    ((name, _args, kwargs),) = manager.calls
    assert name == "create_credential"
    assert kwargs["actor"] == "admin@example.com"
    assert kwargs["allowed_config_versions"] == ["acme-*"]
    assert kwargs["expires_in_days"] == 30 and kwargs["write_tools"] is False


def test_the_schedule_runs_the_sweep(manager):
    manager.module.handler(
        {"source": "aws.events", "detail-type": "Scheduled Event"}, None
    )
    assert [c[0] for c in manager.calls] == ["sweep"]


def test_a_scheduled_looking_event_from_the_api_is_not_a_sweep(manager):
    """The dispatcher builds the resolver event from the request; a request body
    claiming to be a schedule still arrives as an operation, never as a sweep."""
    e = event("listMcpCredentials")
    e["arguments"] = {"source": "aws.events"}
    manager.module.handler(e, None)
    assert [c[0] for c in manager.calls] == ["list_credentials"]


# --- the scoped Admin ------------------------------------------------------------


@pytest.mark.parametrize(
    "requested,allowed",
    [
        (["acme-invoices"], True),
        (["acme-*"], True),
        (["acme-eu-*"], False),
        (["globex-w2"], False),
        (None, False),
    ],
)
def test_a_scoped_admin_grants_only_inside_their_scope(manager, requested, allowed):
    manager.own["scope"] = ["acme-*"]
    e = event(
        "createMcpCredential", name="n", role="Viewer", allowedConfigVersions=requested
    )
    if allowed:
        manager.module.handler(e, None)
        assert manager.calls
    else:
        with pytest.raises(PermissionError, match="Unauthorized"):
            manager.module.handler(e, None)
        assert manager.calls == []


def test_a_scoped_admin_cannot_create_an_admin_credential(manager):
    manager.own["scope"] = ["acme-*"]
    with pytest.raises(PermissionError):
        manager.module.handler(
            event("createMcpCredential", name="n", role="Admin"), None
        )


@pytest.mark.parametrize(
    "field", ["updateMcpCredential", "rotateMcpCredential", "revokeMcpCredential"]
)
def test_a_scoped_admin_cannot_act_on_a_credential_outside_their_scope(manager, field):
    manager.own["scope"] = ["acme-*"]
    manager.rows["c"] = {"clientId": "c", "allowedConfigVersions": ["globex-*"]}
    with pytest.raises(PermissionError):
        manager.module.handler(event(field, clientId="c"), None)
    assert manager.calls == []


def test_a_scoped_admin_cannot_widen_a_credential_by_update(manager):
    manager.own["scope"] = ["acme-*"]
    manager.rows["c"] = {"clientId": "c", "allowedConfigVersions": ["acme-*"]}
    with pytest.raises(PermissionError):
        manager.module.handler(
            event(
                "updateMcpCredential", clientId="c", allowedConfigVersions=["globex-*"]
            ),
            None,
        )
    with pytest.raises(PermissionError):
        manager.module.handler(
            event("updateMcpCredential", clientId="c", clearScope=True), None
        )


def test_require_within_reads_patterns_as_entries_not_matches(manager):
    rw = manager.module.require_within
    rw(["acme-*"], ["acme-*"])
    with pytest.raises(PermissionError):
        rw(["acme-*"], ["acme-*-eu"])
    rw(None, None)


@pytest.mark.parametrize(
    "tenant,current,allowed",
    [
        ("acme", ["acme-*"], True),
        ("globex", ["acme-*"], False),
        ("acme", ["globex-*"], False),
        (None, ["acme-*"], True),
    ],
)
def test_a_scoped_admin_grants_only_a_tenant_inside_their_scope(
    manager, tenant, current, allowed
):
    manager.own["scope"] = ["acme-*"]
    manager.rows["c"] = {"clientId": "c", "allowedConfigVersions": current}
    e = event("setMcpCredentialTenant", clientId="c", tenantId=tenant)
    if allowed:
        manager.module.handler(e, None)
        assert [c[0] for c in manager.calls] == ["set_credential_tenant"]
    else:
        with pytest.raises(PermissionError, match="Unauthorized"):
            manager.module.handler(e, None)
        assert manager.calls == []


def test_an_unscoped_admin_may_grant_any_tenant(manager):
    manager.rows["c"] = {"clientId": "c", "allowedConfigVersions": ["acme-*"]}
    manager.module.handler(
        event("setMcpCredentialTenant", clientId="c", tenantId="globex"), None
    )
    assert [c[0] for c in manager.calls] == ["set_credential_tenant"]


def test_the_acting_admin_is_recorded_by_email(manager):
    manager.module.handler(event("createMcpCredential", name="n", role="Viewer"), None)
    assert manager.calls[0][2]["actor"] == "admin@example.com"


# --- the registry-removal custom resource ------------------------------------------


def cfn(request_type):
    return {
        "RequestType": request_type,
        "ResponseURL": "https://example.invalid/response",
        "StackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/s/1",
        "RequestId": "r",
        "LogicalResourceId": "McpCredentialsCleanup",
        "ResourceType": "Custom::McpCredentialsCleanup",
        "ServiceToken": "arn:aws:lambda:us-east-1:111122223333:function:f",
    }


def test_deleting_the_cleanup_resource_revokes_every_credential(manager):
    manager.module.handler(cfn("Delete"), None)
    assert [c[0] for c in manager.calls] == ["revoke_all"]
    assert manager.sent == [("SUCCESS", "mcp-credentials-cleanup")]


def test_the_cleanup_is_given_the_time_the_invocation_has_left(manager):
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 42_000)
    manager.module.handler(cfn("Delete"), context)
    seconds_left = manager.calls[0][2]["seconds_left"]
    assert seconds_left() == 42.0


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_creating_or_updating_the_cleanup_resource_changes_nothing(
    manager, request_type
):
    manager.module.handler(cfn(request_type), None)
    assert manager.calls == []
    assert manager.sent == [("SUCCESS", "mcp-credentials-cleanup")]


def test_a_failed_cleanup_still_lets_the_delete_finish(manager, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("gateway gone")

    monkeypatch.setattr(manager.module.mc, "revoke_all", boom)
    manager.module.handler(cfn("Delete"), None)
    assert manager.sent == [("SUCCESS", "mcp-credentials-cleanup")]


def test_an_api_request_naming_cloudformation_keys_is_not_a_custom_resource(manager):
    e = event("listMcpCredentials", groups=("Viewer",), **cfn("Delete"))
    with pytest.raises(PermissionError):
        manager.module.handler(e, None)
    assert manager.calls == [] and manager.sent == []


def test_a_scoped_admin_lists_only_credentials_inside_their_scope(manager, monkeypatch):
    listed = [
        {"clientId": "a", "allowedConfigVersions": ["acme-invoices"]},
        {"clientId": "g", "allowedConfigVersions": ["globex-*"]},
        {"clientId": "u"},
    ]
    monkeypatch.setattr(manager.module.mc, "list_credentials", lambda _svc: listed)
    manager.own["scope"] = ["acme-*"]
    rows = manager.module.handler(event("listMcpCredentials"), None)
    assert [r["clientId"] for r in rows] == ["a"]
    manager.own["scope"] = None
    rows = manager.module.handler(event("listMcpCredentials"), None)
    assert [r["clientId"] for r in rows] == ["a", "g", "u"]
