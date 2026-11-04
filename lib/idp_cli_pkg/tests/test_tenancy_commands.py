# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
``idp-cli tenancy``: each command is a thin call into ``client.tenancy``, so what is
tested here is the CLI's own contract — exit codes per outcome, and stdout carrying
only the machine-readable payload.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from idp_cli.cli import cli
from idp_sdk import (
    IDPValidationError,
    TenantApplyResult,
    TenantAuditFinding,
    TenantAuditResult,
    TenantInfo,
    TenantListResult,
    TenantPlanAction,
    TenantPlanItem,
    TenantPolicyResult,
    TenantVerifyCheck,
    TenantVerifyOutcome,
    TenantVerifyResult,
)


@pytest.fixture
def runner():
    # Click 8.2+ keeps stdout and stderr separate, which is what lets a test assert
    # that stdout holds the payload and nothing else.
    return CliRunner()


@pytest.fixture
def client():
    instance = MagicMock()
    with patch("idp_sdk.IDPClient", return_value=instance):
        yield instance


def _invoke(runner, *args):
    return runner.invoke(
        cli, ["tenancy", *args, "--stack-name", "S"], catch_exceptions=False
    )


@pytest.mark.unit
def test_list_json_is_the_only_stdout(runner, client):
    client.tenancy.list.return_value = TenantListResult(
        tenants=[
            TenantInfo(tenant_id="acme", prefixes=["a/"], profile_pattern="acme-*")
        ],
        count=1,
        isolation_mode="Strict",
    )
    result = _invoke(runner, "list", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["isolation_mode"] == "Strict"
    assert payload["tenants"][0]["tenant_id"] == "acme"


@pytest.mark.unit
def test_put_passes_every_prefix_and_reports_a_refusal(runner, client):
    client.tenancy.put.return_value = TenantInfo(
        tenant_id="acme", prefixes=["a/", "b/"], profile_pattern="acme-*"
    )
    ok = _invoke(
        runner,
        "put",
        "--tenant-id",
        "acme",
        "--prefix",
        "a/",
        "--prefix",
        "b/",
        "--profile-pattern",
        "acme-*",
    )
    assert ok.exit_code == 0
    assert client.tenancy.put.call_args.args == ("acme", ["a/", "b/"], "acme-*")

    client.tenancy.put.side_effect = IDPValidationError("overlaps tenant 'globex'")
    refused = _invoke(
        runner, "put", "--tenant-id", "x", "--prefix", "a/", "--profile-pattern", "x-*"
    )
    assert refused.exit_code == 1
    assert "overlaps" in refused.stderr
    assert refused.stdout == ""


@pytest.mark.unit
def test_apply_is_a_dry_run_unless_yes(runner, client, tmp_path):
    path = tmp_path / "tenants.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tenants": [
                    {"tenantId": "acme", "prefixes": ["a/"], "profilePattern": "acme-*"}
                ]
            }
        )
    )
    client.tenancy.apply.return_value = TenantApplyResult(
        plan=[TenantPlanItem(tenant_id="acme", action=TenantPlanAction.CREATE)]
    )
    result = _invoke(runner, "apply", "--file", str(path))
    assert result.exit_code == 0
    assert client.tenancy.apply.call_args.kwargs == {"prune": False, "dry_run": True}
    assert "Dry run" in result.stderr

    _invoke(runner, "apply", "--file", str(path), "--yes", "--prune")
    assert client.tenancy.apply.call_args.kwargs == {"prune": True, "dry_run": False}


@pytest.mark.unit
def test_apply_with_a_refusal_exits_1(runner, client, tmp_path):
    path = tmp_path / "tenants.yaml"
    path.write_text("tenants: []\n")
    client.tenancy.apply.return_value = TenantApplyResult(
        plan=[
            TenantPlanItem(
                tenant_id="bad", action=TenantPlanAction.CREATE, error="overlaps"
            )
        ]
    )
    result = _invoke(runner, "apply", "--file", str(path), "--yes")
    assert result.exit_code == 1
    assert "REFUSED" in result.stderr


@pytest.mark.unit
def test_generate_policies_writes_only_the_template_to_stdout(runner, client, tmp_path):
    principals = tmp_path / "p.yaml"
    principals.write_text("acme:\n  uploaders: [arn:aws:iam::111122223333:role/u]\n")
    client.tenancy.generate_policies.return_value = TenantPolicyResult(
        template="AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n",
        access_points=[],
        warnings=["a warning [with brackets]"],
    )
    result = _invoke(runner, "generate-policies", "--principals", str(principals))
    assert result.exit_code == 0
    assert result.stdout == "AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n"
    assert "a warning [with brackets]" in result.stderr
    assert client.tenancy.generate_policies.call_args.args == (
        {"acme": {"uploaders": ["arn:aws:iam::111122223333:role/u"]}},
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "findings,code",
    [([], 0), ([TenantAuditFinding(kind="k", subject="s", detail="d")], 1)],
)
def test_audit_exits_1_on_findings(runner, client, findings, code):
    client.tenancy.audit.return_value = TenantAuditResult(
        isolation_mode="Strict", findings=findings
    )
    result = _invoke(runner, "audit", "--json")
    assert result.exit_code == code
    assert json.loads(result.stdout)["findings"] == [f.model_dump() for f in findings]


@pytest.mark.unit
@pytest.mark.parametrize(
    "outcomes,expected_code,expected",
    [
        ([TenantVerifyOutcome.PASS], 0, "PASS"),
        ([TenantVerifyOutcome.PASS, TenantVerifyOutcome.SKIP], 3, "SKIP"),
        ([TenantVerifyOutcome.SKIP, TenantVerifyOutcome.ERROR], 2, "ERROR"),
        ([TenantVerifyOutcome.ERROR, TenantVerifyOutcome.FAIL], 1, "FAIL"),
        ([], 2, "ERROR"),
    ],
)
def test_verify_exit_code_per_outcome(
    runner, client, tmp_path, outcomes, expected_code, expected
):
    doc = tmp_path / "d.pdf"
    doc.write_bytes(b"%PDF")
    client.tenancy.verify.return_value = TenantVerifyResult(
        isolation_mode="Strict",
        checks=[
            TenantVerifyCheck(name=f"c{i}", outcome=o) for i, o in enumerate(outcomes)
        ],
    )
    result = _invoke(runner, "verify", "--document", str(doc), "--json")
    assert result.exit_code == expected_code
    assert json.loads(result.stdout)["outcome"] == expected
    assert client.tenancy.verify.call_args.args == (b"%PDF", "d.pdf")


@pytest.mark.unit
def test_put_passes_every_granted_by_value(runner, client):
    client.tenancy.put.return_value = TenantInfo(
        tenant_id="acme",
        prefixes=["a/"],
        profile_pattern="acme-*",
        granted_by=["tenant.acme", "Acme Users"],
    )
    result = _invoke(
        runner,
        "put",
        "--tenant-id",
        "acme",
        "--prefix",
        "a/",
        "--profile-pattern",
        "acme-*",
        "--granted-by",
        "tenant.acme",
        "--granted-by",
        "Acme Users",
    )
    assert result.exit_code == 0
    assert client.tenancy.put.call_args.kwargs["granted_by"] == [
        "tenant.acme",
        "Acme Users",
    ]


@pytest.mark.unit
def test_set_user_tenant_json_is_the_only_stdout(runner, client):
    from idp_sdk import TenantUserGrant

    client.tenancy.set_user_tenant.return_value = TenantUserGrant(
        email="ann@example.com",
        user_id="u1",
        tenant_id="acme",
        allowed_config_versions=["acme-*"],
    )
    result = _invoke(
        runner,
        "set-user-tenant",
        "--email",
        "ann@example.com",
        "--tenant",
        "acme",
        "--json",
    )
    assert result.exit_code == 0
    assert client.tenancy.set_user_tenant.call_args.args == ("ann@example.com", "acme")
    assert json.loads(result.stdout)["allowed_config_versions"] == ["acme-*"]


@pytest.mark.unit
def test_set_user_tenant_without_a_tenant_grants_none(runner, client):
    from idp_sdk import TenantUserGrant

    client.tenancy.set_user_tenant.return_value = TenantUserGrant(
        email="ann@example.com",
        user_id="u1",
        tenant_id=None,
        allowed_config_versions=["!no-tenant-granted"],
    )
    result = _invoke(runner, "set-user-tenant", "--email", "ann@example.com")
    assert result.exit_code == 0
    assert client.tenancy.set_user_tenant.call_args.args == ("ann@example.com", None)
    assert "no tenant" in result.stderr
    assert result.stdout == ""


@pytest.mark.unit
def test_set_user_tenant_refusal_exits_1(runner, client):
    client.tenancy.set_user_tenant.side_effect = IDPValidationError(
        "set_user_tenant is available only when TenantGrantSource is Api"
    )
    result = _invoke(
        runner, "set-user-tenant", "--email", "ann@example.com", "--tenant", "acme"
    )
    assert result.exit_code == 1
    assert "TenantGrantSource is Api" in result.stderr
    assert result.stdout == ""
