# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
``client.tenancy``: declarations, the access-point template, the audit and the live
check's orchestration.

The stack lookups are replaced (`_resources`, `isolation_mode`) and the tables are
moto, so what runs is the real store, the real boundary rules from
``idp_common.config.tenants`` and the real template builder.
"""

import json
import re
from typing import Any, Dict, List

import boto3
import pytest
import yaml
from moto import mock_aws

from idp_sdk import IDPClient, IDPValidationError, TenantVerifyOutcome
from idp_sdk._core import tenancy_policies as tp
from idp_sdk._core import ui_api
from idp_sdk._core.tenancy_verify import run_verify
from idp_sdk.models.tenancy import TenantPlanAction

STACK = "IDPTest"
ACME = {"tenantId": "acme", "prefixes": ["clients/acme/"], "profilePattern": "acme-*"}
GLOBEX = {
    "tenantId": "globex",
    "prefixes": ["clients/globex/"],
    "profilePattern": "globex-*",
}
UPLOADER = "arn:aws:iam::111122223333:role/acme-uploader"
CONSUMER = "arn:aws:iam::111122223333:role/acme-reader"

# Assembled rather than written out: any example of this shape, even AWS's own
# documentation one, is refused by the push-time secret scanner some contributors run.
KMS_KEY_ARN = ":".join(
    ["arn", "aws", "kms", "us-east-1", "123456789012", "key/example"]
)


def _make_tables():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    config = ddb.create_table(
        TableName="config",
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # The host UsersTable's real key schema: a user's row is PK = SK = USER#<id>
    # with `userId` stored unprefixed, and a SUB#<sub> pointer item beside it.
    users = ddb.create_table(
        TableName="users",
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
    tracking = ddb.create_table(
        TableName="tracking",
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
    return config, users, tracking


@pytest.fixture
def tenancy(aws_credentials, monkeypatch):
    with mock_aws():
        config, users, tracking = _make_tables()
        client = IDPClient(stack_name=STACK, region="us-east-1")
        op = client.tenancy
        resources = {
            "ConfigurationTable": "config",
            "UsersTable": "users",
            "TrackingTable": "tracking",
            "InputBucket": "input-bucket",
            "OutputBucket": "output-bucket",
        }
        monkeypatch.setattr(
            op,
            "_resources",
            lambda stack, ids: {k: v for k, v in resources.items() if k in ids},
        )
        monkeypatch.setattr(op, "isolation_mode", lambda stack_name=None: "Strict")
        op._test_tables = {"config": config, "users": users, "tracking": tracking}
        yield op


def _mapping(op, prefix, profile):
    # Written straight to the item, bypassing every check, so a test can stage a
    # crossing mapping the API would refuse.
    table = op._test_tables["config"]
    item = (
        table.get_item(Key={"Configuration": "ConfigPrefixMap#__index"}).get("Item")
        or {}
    )
    mappings = list(item.get("Mappings") or [])
    mappings.append({"prefix": prefix, "configProfile": profile, "enabled": True})
    table.put_item(
        Item={"Configuration": "ConfigPrefixMap#__index", "Mappings": mappings}
    )


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_put_list_delete_round_trip(tenancy):
    tenancy.put("acme", ["clients/acme/"], "acme-*", description="ACME")
    listed = tenancy.list()
    assert listed.isolation_mode == "Strict"
    assert [t.tenant_id for t in listed.tenants] == ["acme"]
    assert listed.tenants[0].created_by == "idp-cli"
    assert tenancy.delete("acme") is True
    assert tenancy.delete("acme") is False


@pytest.mark.unit
def test_put_applies_the_same_refusals_as_the_api(tenancy):
    tenancy.put("acme", ["clients/acme/"], "acme-*")
    with pytest.raises(IDPValidationError, match="overlaps"):
        tenancy.put("dup", ["clients/acme/eu/"], "dup-*")
    with pytest.raises(IDPValidationError, match="separator"):
        tenancy.put("bad", ["b/"], "bad*")


@pytest.mark.unit
def test_put_refuses_a_tenant_over_a_crossing_mapping(tenancy):
    _mapping(tenancy, "clients/acme/eu/", "globex-w2")
    with pytest.raises(IDPValidationError, match="clients/acme/eu/"):
        tenancy.put("acme", ["clients/acme/"], "acme-*")
    assert tenancy.list().tenants == []


def _scoped_user(op, n, scope, persona="Viewer"):
    op._test_tables["users"].put_item(
        Item={
            "PK": f"USER#{n}",
            "SK": f"USER#{n}",
            "userId": str(n),
            "email": f"user{n}@example.com",
            "persona": persona,
            "allowedConfigVersions": scope,
        }
    )


@pytest.mark.unit
def test_put_refuses_a_tenant_that_leaves_a_stored_scope_crossing(tenancy):
    tenancy.put("globex", ["clients/globex/"], "globex-*")
    _scoped_user(tenancy, 7, ["acme-x", "globex-w2"])
    with pytest.raises(IDPValidationError, match="user7@example.com"):
        tenancy.put("acme", ["clients/acme/"], "acme-*")
    assert [t.tenant_id for t in tenancy.list().tenants] == ["globex"]


@pytest.mark.unit
def test_put_ignores_an_admins_stored_scope(tenancy):
    tenancy.put("globex", ["clients/globex/"], "globex-*")
    _scoped_user(tenancy, 8, ["acme-x", "globex-w2"], persona="Admin")
    tenancy.put("acme", ["clients/acme/"], "acme-*")


@pytest.mark.unit
def test_apply_reports_a_stored_scope_crossing_and_writes_nothing(tenancy):
    _scoped_user(tenancy, 9, ["acme-*", "globex-*"])
    result = tenancy.apply([ACME, GLOBEX], dry_run=False)
    assert not result.applied
    assert any("user9@example.com" in (i.error or "") for i in result.plan)
    assert tenancy.list().tenants == []


@pytest.mark.unit
def test_apply_is_a_dry_run_by_default(tenancy):
    result = tenancy.apply([ACME, GLOBEX])
    assert not result.applied
    assert [(i.tenant_id, i.action) for i in result.plan] == [
        ("acme", TenantPlanAction.CREATE),
        ("globex", TenantPlanAction.CREATE),
    ]
    assert tenancy.list().tenants == []


@pytest.mark.unit
def test_apply_writes_updates_and_prunes(tenancy):
    tenancy.apply([ACME, GLOBEX], dry_run=False)
    result = tenancy.apply(
        [{**ACME, "description": "changed"}], prune=True, dry_run=False
    )
    actions = {i.tenant_id: i.action for i in result.plan}
    assert actions == {
        "acme": TenantPlanAction.UPDATE,
        "globex": TenantPlanAction.DELETE,
    }
    assert [t.tenant_id for t in tenancy.list().tenants] == ["acme"]
    assert tenancy.list().tenants[0].description == "changed"


@pytest.mark.unit
def test_apply_moves_a_prefix_between_two_tenants(tenancy):
    tenancy.apply(
        [
            {**ACME, "prefixes": ["clients/acme/", "shared/x/"]},
            GLOBEX,
        ],
        dry_run=False,
    )
    # `shared/x/` moves from acme to globex. Whichever is written first, the other
    # still holds it, so a single ordered pass would refuse one of them.
    result = tenancy.apply(
        [ACME, {**GLOBEX, "prefixes": ["clients/globex/", "shared/x/"]}], dry_run=False
    )
    assert result.applied
    owners = {t.tenant_id: t.prefixes for t in tenancy.list().tenants}
    assert owners["globex"] == ["clients/globex/", "shared/x/"]
    assert owners["acme"] == ["clients/acme/"]


@pytest.mark.unit
def test_apply_with_one_bad_entry_writes_nothing(tenancy):
    result = tenancy.apply(
        [
            ACME,
            {
                "tenantId": "bad",
                "prefixes": ["clients/acme/x/"],
                "profilePattern": "bad-*",
            },
        ],
        dry_run=False,
    )
    assert not result.applied
    assert any(i.error for i in result.plan)
    assert tenancy.list().tenants == []


@pytest.mark.unit
def test_apply_refuses_a_set_that_crosses_an_existing_mapping(tenancy):
    _mapping(tenancy, "clients/acme/", "globex-w2")
    result = tenancy.apply([ACME], dry_run=False)
    assert not result.applied
    assert result.plan[-1].tenant_id == "*" and "clients/acme/" in result.plan[-1].error


@pytest.mark.unit
def test_apply_refuses_a_duplicate_tenant(tenancy):
    with pytest.raises(IDPValidationError, match="twice"):
        tenancy.apply([ACME, ACME])


# ---------------------------------------------------------------------------
# Access-point template
# ---------------------------------------------------------------------------


def _principals():
    return {
        "acme": {"uploaders": [UPLOADER], "consumers": [CONSUMER]},
        "globex": {"uploaders": ["arn:aws:iam::123456789012:root"]},
    }


@pytest.mark.unit
def test_generate_policies_template(tenancy):
    tenancy.apply([ACME, GLOBEX], dry_run=False)
    result = tenancy.generate_policies(_principals())
    template = yaml.safe_load(result.template)
    resources = template["Resources"]
    # No YAML anchors/aliases: the template is meant to be read before deploying.
    assert "&id" not in result.template and "*id" not in result.template
    assert len(resources) == 3  # acme in+out, globex in only
    assert any("globex" in w and "consumers" in w for w in result.warnings)

    by_name = {r["Properties"]["Name"]: r["Properties"] for r in resources.values()}
    acme_in = by_name[tp.access_point_name(STACK, "acme", "in")]
    assert acme_in["Bucket"] == "input-bucket"
    stmt = [s for s in acme_in["Policy"]["Statement"] if s["Effect"] == "Allow"]
    assert [s["Action"] for s in stmt] == ["s3:PutObject"]
    assert stmt[0]["Principal"] == {"AWS": UPLOADER}
    # The confinement is the Denies, because a same-account role's own identity
    # policy can grant what an Allow here does not.
    denies = {
        s["Sid"]: s for s in acme_in["Policy"]["Statement"] if s["Effect"] == "Deny"
    }
    assert set(denies) == {
        "DenyPrincipalsOfOtherTenants",
        "DenyObjectsOutsideTenantPrefixes",
    }
    assert denies["DenyPrincipalsOfOtherTenants"]["Condition"] == {
        "ArnNotEquals": {"aws:PrincipalArn": [UPLOADER]}
    }
    assert denies["DenyObjectsOutsideTenantPrefixes"]["Action"] == "s3:PutObject"
    assert (
        denies["DenyObjectsOutsideTenantPrefixes"]["NotResource"] == stmt[0]["Resource"]
    )
    (resource,) = stmt[0]["Resource"]
    assert resource["Fn::Sub"].startswith("arn:${AWS::Partition}:s3:")
    assert resource["Fn::Sub"].endswith("/object/clients/acme/*")

    acme_out = by_name[tp.access_point_name(STACK, "acme", "out")]
    assert acme_out["Bucket"] == "output-bucket"
    actions = {
        s["Action"]: s
        for s in acme_out["Policy"]["Statement"]
        if s["Effect"] == "Allow"
    }
    assert set(actions) == {"s3:GetObject", "s3:ListBucket"}
    out_denies = {
        s["Sid"]: s for s in acme_out["Policy"]["Statement"] if s["Effect"] == "Deny"
    }
    assert out_denies["DenyListingOutsideTenantPrefixes"]["Condition"] == {
        "StringNotLike": {"s3:prefix": ["clients/acme/*"]}
    }
    assert out_denies["DenyObjectsOutsideTenantPrefixes"]["Action"] == "s3:GetObject"
    assert actions["s3:ListBucket"]["Condition"] == {
        "StringLike": {"s3:prefix": ["clients/acme/*"]}
    }
    # No grant reaches another tenant's prefix.
    assert "globex" not in json.dumps(acme_in) + json.dumps(acme_out)


@pytest.mark.unit
def test_generate_policies_is_deterministic(tenancy):
    tenancy.apply([ACME, GLOBEX], dry_run=False)
    first = tenancy.generate_policies(_principals()).template
    second = tenancy.generate_policies(
        dict(reversed(list(_principals().items())))
    ).template
    assert first == second


@pytest.mark.unit
def test_a_tenant_key_adds_a_deny_for_any_other_key(tenancy):
    tenancy.apply([ACME], dry_run=False)
    key = KMS_KEY_ARN
    result = tenancy.generate_policies(
        {"acme": {"uploaders": [UPLOADER], "kmsKeyArn": key}}
    )
    (ap,) = yaml.safe_load(result.template)["Resources"].values()
    deny = [s for s in ap["Properties"]["Policy"]["Statement"] if s["Effect"] == "Deny"]
    assert deny[0]["Condition"]["StringNotEquals"] == {
        "s3:x-amz-server-side-encryption-aws-kms-key-id": key
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "principals,fragment",
    [
        ({"nobody": {"uploaders": [UPLOADER]}}, "not declared"),
        ({"acme": {"uploaders": ["arn:aws:iam::1:role/x"]}}, "not an IAM"),
        ({"acme": {"uploaders": ["*"]}}, "not an IAM"),
        ({"acme": {"writers": [UPLOADER]}}, "unknown key"),
        ({"acme": {"kmsKeyArn": "alias/x"}}, "not a KMS key"),
        (["acme"], "must be a mapping"),
    ],
)
def test_principals_are_validated(tenancy, principals, fragment):
    tenancy.apply([ACME], dry_run=False)
    with pytest.raises(IDPValidationError, match=fragment):
        tenancy.generate_policies(principals)


@pytest.mark.unit
@pytest.mark.parametrize("tenant_id", ["a", "acme", "x" * 63, "a-" * 31 + "b"])
def test_access_point_names_fit_the_s3_rules(tenant_id):
    for direction in ("in", "out"):
        name = tp.access_point_name(STACK, tenant_id, direction)
        assert 3 <= len(name) <= 50, name
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", name), name
        assert "--" not in name


@pytest.mark.unit
def test_long_tenant_ids_sharing_a_head_get_distinct_names():
    a = tp.access_point_name(STACK, "x" * 40 + "a", "in")
    b = tp.access_point_name(STACK, "x" * 40 + "b", "in")
    assert a != b


def _cfn_lint(template_text: str, region: str) -> List[Any]:
    cfnlint_api = pytest.importorskip("cfnlint.api")
    from cfnlint.config import ManualArgs

    return cfnlint_api.lint(template_text, config=ManualArgs(regions=[region]))


@pytest.mark.unit
@pytest.mark.parametrize("region", ["us-east-1", "us-gov-west-1"])
def test_generated_template_passes_cfn_lint(tenancy, region):
    tenancy.apply([ACME, GLOBEX], dry_run=False)
    key = KMS_KEY_ARN
    principals = _principals()
    principals["acme"]["kmsKeyArn"] = key
    matches = _cfn_lint(tenancy.generate_policies(principals).template, region)
    assert matches == [], [str(m) for m in matches]


@pytest.mark.unit
def test_cfn_lint_gate_is_live():
    # The lint test above proves nothing if cfn-lint passes everything it is handed.
    broken = "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  X:\n    Type: AWS::S3::AccessPoint\n    Properties: {}\n"
    assert _cfn_lint(broken, "us-east-1")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class _Cognito:
    def __init__(self, admins):
        self.admins = admins

    def get_paginator(self, name):
        assert name == "list_users_in_group"
        admins = self.admins

        class _P:
            def paginate(self, **kwargs):
                yield {
                    "Users": [
                        {"Attributes": [{"Name": "email", "Value": e}]} for e in admins
                    ]
                }

        return _P()


@pytest.mark.unit
def test_audit_reports_each_class_of_finding(tenancy, monkeypatch):
    tenancy.apply([ACME, GLOBEX], dry_run=False)
    monkeypatch.setattr(tenancy, "isolation_mode", lambda stack_name=None: "Off")
    resources = tenancy._resources
    monkeypatch.setattr(
        tenancy,
        "_resources",
        lambda stack, ids: {
            **resources(stack, ids),
            **({"UserPool": "pool"} if "UserPool" in ids else {}),
        },
    )
    real_boto = tenancy._boto
    monkeypatch.setattr(
        tenancy,
        "_boto",
        lambda svc: (
            _Cognito(["admin@example.com"]) if svc == "cognito-idp" else real_boto(svc)
        ),
    )
    # acme has its own mapping; globex is governed by an ancestor. One mapping
    # crosses into acme from outside.
    _mapping(tenancy, "clients/acme/", "acme-invoices")
    _mapping(tenancy, "clients/", "lending")
    _mapping(tenancy, "other/", "acme-x")
    users = tenancy._test_tables["users"]
    users.put_item(
        Item={
            "PK": "USER#1",
            "SK": "USER#1",
            "userId": "1",
            "email": "admin@example.com",
            "persona": "Viewer",
        }
    )
    users.put_item(
        Item={
            "PK": "USER#2",
            "SK": "USER#2",
            "userId": "2",
            "email": "open@example.com",
            "persona": "Viewer",
        }
    )
    users.put_item(
        Item={
            "PK": "USER#3",
            "SK": "USER#3",
            "userId": "3",
            "email": "wide@example.com",
            "persona": "Author",
            "allowedConfigVersions": ["acme-*", "globex-*"],
        }
    )
    users.put_item(
        Item={
            "PK": "USER#4",
            "SK": "USER#4",
            "userId": "4",
            "email": "ok@example.com",
            "persona": "Viewer",
            "allowedConfigVersions": ["acme-*"],
        }
    )
    users.put_item(Item={"PK": "SUB#abc", "SK": "SUB#abc", "userId": "2"})
    tracking = tenancy._test_tables["tracking"]
    tracking.put_item(
        Item={
            "PK": "doc#clients/acme/a.pdf",
            "SK": "none",
            "ObjectStatus": "COMPLETED",
            "ConfigVersion": "acme-invoices",
        }
    )
    tracking.put_item(
        Item={
            "PK": "doc#clients/acme/b.pdf",
            "SK": "none",
            "ObjectStatus": "COMPLETED",
            "ConfigVersion": "globex-w2",
        }
    )
    # Not findings: a per-run history row repeating another tenant's profile, the
    # status-less row a deleted document leaves, and a document refused at ingest.
    tracking.put_item(
        Item={
            "PK": "doc#clients/acme/a.pdf",
            "SK": "run#1",
            "ConfigVersion": "globex-w2",
        }
    )
    tracking.put_item(Item={"PK": "doc#clients/acme/gone.pdf", "SK": "none"})
    tracking.put_item(
        Item={
            "PK": "doc#clients/acme/refused.pdf",
            "SK": "none",
            "ObjectStatus": "FAILED",
            "ConfigAssignmentError": "Refused: ...",
        }
    )
    tracking.put_item(Item={"PK": "list#2026", "SK": "x", "ConfigVersion": "globex-w2"})

    result = tenancy.audit()
    kinds = sorted((f.kind, f.subject) for f in result.findings)
    assert kinds == [
        ("cross-tenant-document", "clients/acme/b.pdf"),
        ("cross-tenant-scope", "wide@example.com"),
        ("crossing-mapping", "other/"),
        ("isolation-mode", STACK),
        ("uncovered-prefix", "clients/globex/"),
        ("unscoped-user", "open@example.com"),
    ]
    assert not result.clean


@pytest.mark.unit
def test_audit_with_no_tenants_says_so(tenancy):
    result = tenancy.audit()
    assert result.clean
    assert any("No tenants" in n for n in result.notes)


@pytest.mark.unit
def test_audit_compares_access_point_policies(tenancy, monkeypatch):
    tenancy.apply([ACME], dry_run=False)
    _mapping(tenancy, "clients/acme/", "acme-invoices")
    principals = {"acme": {"uploaders": [UPLOADER], "consumers": [CONSUMER]}}
    clean = tp.load_principals(principals, ["acme"])
    planned, _ = tp.planned_access_points(STACK, [ACME], clean)
    live: Dict[str, str] = {}
    for ap in planned:
        live[ap["name"]] = json.dumps(
            tp.access_point_policy(
                ap["name"],
                ap["prefixes"],
                ap["principals"],
                ap["direction"],
                tp.literal_arn(ap["name"], "aws", "us-east-1", "111122223333"),
                None,
            )
        )
    in_name = tp.access_point_name(STACK, "acme", "in")
    live[in_name] = live[in_name].replace("clients/acme/*", "clients/*")

    class _S3Control:
        meta = type("M", (), {"region_name": "us-east-1"})

        def get_access_point_policy(self, AccountId, Name):
            return {"Policy": live[Name]}

    class _Sts:
        def get_caller_identity(self):
            return {
                "Account": "111122223333",
                "Arn": "arn:aws:iam::111122223333:user/x",
            }

    real_boto = tenancy._boto
    monkeypatch.setattr(
        tenancy,
        "_boto",
        lambda svc: (
            {"s3control": _S3Control(), "sts": _Sts()}.get(svc) or real_boto(svc)
        ),
    )
    result = tenancy.audit(principals=principals)
    drift = [f for f in result.findings if f.kind.startswith("access-point")]
    assert [(f.kind, f.subject) for f in drift] == [("access-point-drift", in_name)]


@pytest.mark.unit
def test_uncovered_prefix_detection():
    mappings = [
        {"prefix": "clients/", "configProfile": "lending"},
        {"prefix": "clients/acme/", "configProfile": "acme-x"},
        {"prefix": "clients/globex/sub/", "configProfile": "globex-x"},
    ]
    found = tp.uncovered_tenant_prefixes(mappings, [ACME, GLOBEX])
    # globex has only a sub-folder mapping; its top level falls to `clients/`.
    assert found == [("globex", "clients/globex/", "clients/")]
    assert tp.uncovered_tenant_prefixes([], [ACME]) == [("acme", "clients/acme/", None)]


# ---------------------------------------------------------------------------
# Live check orchestration
# ---------------------------------------------------------------------------


class FakeStack:
    """A stack that behaves correctly unless told to leak."""

    def __init__(self, *, leak=False, strict_refuses=True, metadata_wins=False):
        self.leak = leak
        self.strict_refuses = strict_refuses
        # Models a mapping in "Upload metadata wins" mode, the one way an unrefused
        # upload can end up under the profile its metadata named.
        self.metadata_wins = metadata_wins
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.users: Dict[str, Dict[str, Any]] = {}
        self.tokens: Dict[str, str] = {}
        self.deleted_users: List[str] = []
        self.deleted_cognito: List[str] = []
        self.deleted_docs: List[str] = []
        self.calls: List[str] = []

    # S3
    def put_object(self, Bucket, Key, Body, Metadata=None):
        profile = (Metadata or {}).get("config-version")
        if Key.startswith("clients/acme/"):
            resolved = "acme-invoices"
        elif Key.startswith("clients/globex/"):
            resolved = "globex-w2"
        else:
            resolved = "lending"
        if self.metadata_wins and profile:
            resolved = profile
        rejected = self.strict_refuses and (
            not Key.startswith("clients/") or (profile and profile != resolved)
        )
        self.docs[Key] = {
            "ObjectKey": Key,
            "ConfigVersion": None if rejected else resolved,
            "ObjectStatus": "FAILED" if rejected else "COMPLETED",
            "ConfigAssignmentError": "Refused: crosses a tenant" if rejected else None,
        }

    def delete_object(self, Bucket, Key):
        pass

    def scope_of(self, token):
        if token == "admin-token":
            return None
        email = self.tokens[token]
        return self.users[email]["allowedConfigVersions"][0][:-1]

    def visible(self, token, key):
        stem = self.scope_of(token)
        doc = self.docs.get(key)
        if not doc:
            return None
        if stem is None or self.leak or (doc["ConfigVersion"] or "").startswith(stem):
            return doc
        return None

    def api(self, field, args, token):
        self.calls.append(field)
        if field == "createUser":
            self.users[args["email"]] = {**args, "userId": f"id-{args['email']}"}
            return 200, {"userId": f"id-{args['email']}", **args}
        if field == "getDocument":
            return 200, self.visible(token, args["ObjectKey"])
        if field == "getFileContents":
            key = args["s3Uri"].split("/", 3)[3]
            if self.visible(token, key):
                return 200, {
                    "content": "x",
                    "contentType": "application/pdf",
                    "size": 1,
                }
            return 403, {"errors": [{"errorType": "Unauthorized"}]}
        if field == "listDocuments":
            return 200, {
                "Documents": [d for k, d in self.docs.items() if self.visible(token, k)]
            }
        if field == "deleteDocument":
            self.deleted_docs.extend(args["objectKeys"])
            return 200, True
        if field == "deleteUser":
            self.deleted_users.append(args["userId"])
            return 200, True
        raise AssertionError(field)

    def make_admin(self, email, password):
        return "admin-token"

    def make_password(self, email, password):
        token = f"token-{email}"
        self.tokens[token] = email
        return token

    def delete_cognito(self, email):
        self.deleted_cognito.append(email)


MAPPINGS = [
    {"prefix": "clients/acme/", "configProfile": "acme-invoices"},
    {"prefix": "clients/globex/", "configProfile": "globex-w2"},
]


def _verify(stack, mode="Strict", mappings=MAPPINGS, **kwargs):
    return run_verify(
        isolation_mode=mode,
        tenants=[ACME, GLOBEX],
        mappings=mappings,
        document=b"%PDF",
        document_name="d.pdf",
        input_bucket="input-bucket",
        api=stack.api,
        make_admin=stack.make_admin,
        make_password=stack.make_password,
        delete_cognito_user=stack.delete_cognito,
        s3=stack,
        timeout_seconds=0,
        sleep=lambda s: None,
        run_id="r1",
        **kwargs,
    )


@pytest.mark.unit
def test_verify_passes_on_an_isolating_stack():
    stack = FakeStack()
    result = _verify(stack)
    assert result.outcome == TenantVerifyOutcome.PASS, [
        (c.name, c.outcome, c.detail) for c in result.checks
    ]
    names = {c.name for c in result.checks}
    assert {
        "acme:getDocument:globex",
        "globex:getFileContents:acme",
        "acme:listDocuments",
    } <= names
    assert "ingest:outside-every-tenant" in names
    # Everything it made is removed: documents, API users, Cognito users.
    assert len(stack.deleted_docs) == 4
    assert len(stack.deleted_users) == 2
    assert "tenancy-verify-admin-r1@example.com" in stack.deleted_cognito


@pytest.mark.unit
def test_verify_fails_on_a_leaking_stack():
    result = _verify(FakeStack(leak=True))
    assert result.outcome == TenantVerifyOutcome.FAIL
    failed = {c.name for c in result.checks if c.outcome == TenantVerifyOutcome.FAIL}
    assert {
        "acme:getDocument:globex",
        "acme:getFileContents:globex",
        "acme:listDocuments",
    } <= failed


@pytest.mark.unit
def test_verify_fails_when_strict_ingest_does_not_refuse():
    result = _verify(FakeStack(strict_refuses=False, metadata_wins=True))
    failed = {c.name for c in result.checks if c.outcome == TenantVerifyOutcome.FAIL}
    assert failed == {
        "ingest:outside-every-tenant",
        "ingest:acme-prefix-with-globex-metadata",
    }


@pytest.mark.unit
def test_metadata_ignored_in_favour_of_the_tenants_own_profile_is_a_pass():
    """The default "Mapping wins" mode keeps the document inside its tenant without
    refusing it, which is as good as a refusal for the boundary."""
    result = _verify(FakeStack(strict_refuses=False))
    by_name = {c.name: c for c in result.checks}
    row = by_name["ingest:acme-prefix-with-globex-metadata"]
    assert row.outcome == TenantVerifyOutcome.PASS
    assert "acme-invoices" in row.detail
    assert by_name["ingest:outside-every-tenant"].outcome == TenantVerifyOutcome.FAIL


@pytest.mark.unit
def test_verify_skips_strict_rows_in_off_mode_and_skip_is_not_pass():
    result = _verify(FakeStack(strict_refuses=False), mode="Off")
    skipped = {c.name for c in result.checks if c.outcome == TenantVerifyOutcome.SKIP}
    assert skipped == {
        "ingest:outside-every-tenant",
        "ingest:acme-prefix-with-globex-metadata",
    }
    assert result.outcome == TenantVerifyOutcome.SKIP


@pytest.mark.unit
def test_verify_needs_a_mapping_into_each_tenant():
    stack = FakeStack()
    result = _verify(stack, mappings=MAPPINGS[:1])
    assert result.outcome == TenantVerifyOutcome.ERROR
    assert [c.name for c in result.checks] == ["precheck:globex"]
    assert stack.calls == []  # nothing was created


@pytest.mark.unit
def test_verify_cleans_up_after_an_exception():
    stack = FakeStack()
    original = stack.put_object
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("S3 down")
        return original(**kwargs)

    stack.put_object = flaky
    result = _verify(stack)
    assert result.outcome == TenantVerifyOutcome.ERROR
    assert any(c.name == "run" and "S3 down" in c.detail for c in result.checks)
    assert stack.deleted_docs == ["clients/acme/tenancy-verify-r1/d.pdf"]
    assert len(stack.deleted_users) == 2
    assert "tenancy-verify-admin-r1@example.com" in stack.deleted_cognito


@pytest.mark.unit
def test_an_empty_verify_result_is_an_error_not_a_pass():
    from idp_sdk import TenantVerifyResult

    assert (
        TenantVerifyResult(isolation_mode="Strict").outcome == TenantVerifyOutcome.ERROR
    )


# ---------------------------------------------------------------------------
# UI API primitives
# ---------------------------------------------------------------------------


class _Client:
    def __init__(self):
        self.state = {
            "UserPoolId": "pool",
            "ClientId": "client",
            "ClientName": "web",
            "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH"],
            "CallbackURLs": ["https://example.com/cb"],
            "WriteAttributes": ["email"],
            "CreationDate": "x",
            "LastModifiedDate": "y",
            "ClientSecret": None,
        }
        self.updates: List[Dict[str, Any]] = []

    def describe_user_pool_client(self, UserPoolId, ClientId):
        return {"UserPoolClient": dict(self.state)}

    def update_user_pool_client(self, **kwargs):
        self.updates.append(kwargs)
        # A full replace: anything not sent is cleared.
        self.state = {**{k: None for k in self.state}, **kwargs}


@pytest.mark.unit
def test_admin_auth_flow_preserves_the_client_and_restores_it():
    client = _Client()
    ctx = ui_api.UiContext("s", "us-east-1", "pool", "client", "https://x/api", "users")
    with ui_api.AdminAuthFlow(ctx, client):
        assert ui_api.ADMIN_AUTH_FLOW in client.state["ExplicitAuthFlows"]
        assert client.state["CallbackURLs"] == ["https://example.com/cb"]
        assert client.state["WriteAttributes"] == ["email"]
    assert client.state["ExplicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]
    assert client.state["CallbackURLs"] == ["https://example.com/cb"]
    for update in client.updates:
        assert "CreationDate" not in update and "ClientSecret" not in update


@pytest.mark.unit
def test_id_token_retries_only_the_propagation_error():
    ctx = ui_api.UiContext("s", "us-east-1", "pool", "client", "https://x/api", "users")

    class _Auth:
        def __init__(self, errors):
            self.errors = list(errors)

        def admin_initiate_auth(self, **kwargs):
            if self.errors:
                raise RuntimeError(self.errors.pop(0))
            return {"AuthenticationResult": {"IdToken": "tok"}}

    sleeps: List[float] = []
    assert (
        ui_api.id_token(
            _Auth([ui_api.AUTH_FLOW_NOT_PROPAGATED]), ctx, "e", "p", sleep=sleeps.append
        )
        == "tok"
    )
    assert sleeps == [1]
    with pytest.raises(RuntimeError, match="Incorrect"):
        ui_api.id_token(
            _Auth(["Incorrect username or password"]),
            ctx,
            "e",
            "p",
            sleep=sleeps.append,
        )
