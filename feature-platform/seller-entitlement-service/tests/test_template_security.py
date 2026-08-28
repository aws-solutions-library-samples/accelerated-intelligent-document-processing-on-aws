# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static security assertions on the Seller Entitlement Service template.

The service's security properties live mostly in CloudFormation, not in Python:
whether the API requires SigV4, how widely the resource policy opens it, and how
narrow the Lambda's IAM is. Unit tests on the handler cannot see any of that, and
`cfn-lint` checks syntax rather than intent — so a one-line template edit could
silently turn `AWS_IAM` into `NONE`, or `dynamodb:UpdateItem` into `dynamodb:*`,
with every existing test still green.

These tests are the guard for those invariants. Each asserts something whose
violation would be a real vulnerability, and says why in the failure message.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

_TEMPLATE = Path(__file__).resolve().parents[1] / "template.yaml"


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation's short-form intrinsic tags.

    `!Sub`, `!GetAtt`, `!FindInMap` etc. are not standard YAML, so SafeLoader
    rejects them. We only need the document's shape, so every unknown tag
    collapses to a plain string/list.

    ⚠️ The flat-string rendering is load-bearing, not cosmetic. Several
    assertions below read `str(value)` and match on its *ends* — e.g. the
    `endswith("/*")` wildcard check on the resource policy. Wrapping an
    intrinsic in a `{"!Tag": value}` dict (as the repo's other CFN loaders do)
    silently makes those checks unfalsifiable, because `str()` of a dict always
    ends in `}`. `test_the_wildcard_guard_is_live` pins that precondition.

    This cannot execute the document it parses, for two independent reasons:
    SafeLoader (never yaml.Loader/FullLoader) registers no `python/object`,
    `python/name` or `python/object/apply` constructor, so nothing here can
    instantiate an object or import a module; and `_any_tag` returns only plain
    strings, lists and dicts. `test_loader_cannot_execute_its_input` asserts
    both, since with the `yaml.load()` call shape gone no scanner will.
    """


def _any_tag(loader: Any, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return f"!{tag_suffix} {node.value}"
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnLoader.add_multi_constructor("", _any_tag)


@pytest.fixture(scope="module")
def template() -> dict:
    # The loader is driven directly rather than through
    # `yaml.load(..., Loader=_CfnLoader)`. Identical behaviour — that is what
    # yaml.load does internally — minus the call shape that pattern-based
    # scanners report as unsafe deserialization regardless of the loader's
    # actual base class. See idp_sdk._core.cfn_yaml for the shared version of
    # this loader; it is not imported here because ruff.toml bans reaching into
    # idp_sdk._core from outside the SDK, and this service deploys standalone
    # into a seller account.
    loader = _CfnLoader(_TEMPLATE.read_text(encoding="utf-8"))
    try:
        return loader.get_single_data() or {}
    finally:
        loader.dispose()


@pytest.fixture(scope="module")
def resources(template) -> dict:
    return template["Resources"]


def _statements(resource: dict, policy_name: str) -> list:
    for policy in resource["Properties"].get("Policies") or []:
        if policy.get("PolicyName") == policy_name:
            doc = policy["PolicyDocument"]
            return doc["Statement"]
    raise AssertionError(f"policy {policy_name!r} not found")


def _actions(statement: dict) -> list:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


# ---------------------------------------------------------------------------
# API authentication — the single most important property.
# ---------------------------------------------------------------------------


def test_api_requires_sigv4(resources):
    """Without AWS_IAM the endpoint is anonymous, and the Lambda's whole identity
    model collapses: it reads the buyer account from requestContext.identity,
    which API Gateway only populates for a verified SigV4 caller. Anonymous
    access would mean tokens minted for an unauthenticated stranger."""
    auth = resources["ActivationApi"]["Properties"]["Auth"]
    assert auth["DefaultAuthorizer"] == "AWS_IAM"


def test_no_method_disables_authorization(template):
    """`Authorization: NONE` / `AuthorizationType: NONE` anywhere would bypass
    the authorizer for that method."""
    raw = _TEMPLATE.read_text(encoding="utf-8")
    for needle in ("Authorization: NONE", "AuthorizationType: NONE", "Auth: NONE"):
        assert needle not in raw, f"found {needle!r} — that makes the API anonymous"


def test_api_does_not_invoke_the_backend_with_caller_credentials(resources):
    """`InvokeRole: NONE` is load-bearing, in two ways.

    SAM defaults `InvokeRole` to `CALLER_CREDENTIALS` whenever the authorizer is
    `AWS_IAM`, setting the integration credentials to `arn:aws:iam::*:user/*`.
    That combination is (a) rejected by API Gateway at deploy time when a resource
    policy is also set — `CreateDeployment` 400s with "Caller provided credentials
    not allowed when resource policy is set", rolling the whole stack back — and
    (b) unworkable regardless, because invoking as the caller would require every
    buyer account to hold `lambda:InvokeFunction` on the seller's function.

    Both failures are silent in review and neither is visible to a handler unit
    test, so assert the property directly. See
    aws/serverless-application-model#1708.
    """
    auth = resources["ActivationApi"]["Properties"]["Auth"]
    assert auth.get("InvokeRole") == "NONE", (
        "InvokeRole must be NONE. Omitting it makes SAM pass caller credentials to "
        "the integration, which fails the deployment (resource policy conflict) and "
        "would require buyers to hold lambda:InvokeFunction on the seller's function"
    )


def test_the_wildcard_guard_is_live(resources):
    """Guard the guard: the wildcard check below can be silently neutered.

    `test_resource_policy_is_scoped_to_the_activate_method_only` asserts
    `not str(stmt["Resource"]).endswith("/*")`. That is only falsifiable while
    the loader renders an intrinsic as a flat string. Switch the loader to the
    `{"!Tag": value}` convention the repo's other CFN loaders use and `str()`
    ends in `}` for every possible template — the check passes forever, on a
    statement whose Principal is `*`.

    This is not hypothetical: it happened during the review of PR #672 and was
    caught by mutation-testing the template, not by the suite. Assert the
    precondition directly so the next loader change fails here instead.
    """
    resource = resources["ActivationApi"]["Properties"]["Auth"]["ResourcePolicy"][
        "CustomStatements"
    ][0]["Resource"]
    assert isinstance(resource, str), (
        f"Resource parsed as {type(resource).__name__}, not str — the "
        "endswith('/*') wildcard check below is now unfalsifiable. Either keep "
        "the loader's flat-string rendering or rewrite that check to read the "
        "intrinsic's value."
    )


def test_loader_cannot_execute_its_input():
    """The `yaml.load()` call shape is gone, so no scanner watches this loader
    any more. Assert the safety property it used to (over-)flag: SafeLoader
    ancestry, and a real `python/object/apply` payload staying inert.

    A future `class _CfnLoader(yaml.Loader)` — the tempting one-word fix when a
    tag won't parse — turns this file into an actual RCE. This is what fails.
    """
    assert issubclass(_CfnLoader, yaml.SafeLoader)

    marker = Path(tempfile.gettempdir()) / "seller_entitlement_yaml_probe"
    marker.unlink(missing_ok=True)
    payload = f"a: !!python/object/apply:os.system ['touch {marker}']"

    loader = _CfnLoader(payload)
    try:
        result = loader.get_single_data()
    finally:
        loader.dispose()

    assert isinstance(result["a"], (str, list, dict)), (
        f"payload constructed a {type(result['a']).__name__} — the loader is "
        "no longer inert"
    )
    assert not marker.exists(), "the loader executed its input — this is an RCE"


def test_resource_policy_is_scoped_to_the_activate_method_only(resources):
    """`Principal: '*'` is intended (any AWS account may *attempt* activation),
    but the Resource must pin it to POST /activate. A wildcard resource would
    open any future route — including an admin one — to the whole world."""
    statements = resources["ActivationApi"]["Properties"]["Auth"]["ResourcePolicy"][
        "CustomStatements"
    ]
    assert len(statements) == 1
    stmt = statements[0]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == "execute-api:Invoke"
    resource = str(stmt["Resource"])
    assert "POST/activate" in resource, (
        f"resource policy is not pinned to POST /activate: {resource!r}"
    )
    assert not resource.rstrip().endswith("/*"), (
        "resource policy ends in a wildcard path — it would cover every route"
    )


def test_api_is_throttled(resources):
    """The endpoint is reachable by any AWS principal on the internet, and every
    request costs a Marketplace SearchAgreements call. An unthrottled stage is a
    cost- and quota-exhaustion target."""
    (settings,) = resources["ActivationApi"]["Properties"]["MethodSettings"]
    assert settings["ThrottlingRateLimit"] > 0
    assert settings["ThrottlingBurstLimit"] > 0


def test_access_logging_is_enabled(resources):
    """The access log is the record of who called, including calls refused before
    they reach the Lambda."""
    access_log = resources["ActivationApi"]["Properties"]["AccessLogSetting"]
    assert access_log["DestinationArn"]
    assert "identity.accountId" in access_log["Format"], (
        "the verified caller account must be logged, or refusals are anonymous"
    )


# ---------------------------------------------------------------------------
# Lambda IAM — least privilege.
# ---------------------------------------------------------------------------


def test_lambda_role_has_no_wildcard_actions(resources):
    role = resources["ActivateFunctionRole"]
    for policy in role["Properties"]["Policies"]:
        for stmt in policy["PolicyDocument"]["Statement"]:
            for action in _actions(stmt):
                assert action != "*", f"wildcard action in {policy['PolicyName']}"
                assert not action.endswith(":*"), (
                    f"service-wide wildcard {action!r} in {policy['PolicyName']}"
                )


def test_marketplace_grants_are_read_only(resources):
    """A metering or catalog-write action here would let a compromised function
    bill customers or alter the listing. Reads only."""
    (stmt,) = _statements(
        resources["ActivateFunctionRole"], "MarketplaceSellerEntitlementRead"
    )
    forbidden_verbs = ("Put", "Batch", "Create", "Update", "Delete", "Start", "Meter")
    for action in _actions(stmt):
        assert action.startswith("aws-marketplace:")
        verb = action.split(":", 1)[1]
        assert not any(verb.startswith(v) for v in forbidden_verbs), (
            f"{action!r} is not a read — a compromised function could use it"
        )


def test_kms_grant_is_sign_only_on_the_one_key(resources):
    """Anything beyond Sign — PutKeyPolicy, ScheduleKeyDeletion, GetPublicKey —
    either lets a compromised function re-point trust or is simply unused."""
    (stmt,) = _statements(resources["ActivateFunctionRole"], "SignActivationTokens")
    assert _actions(stmt) == ["kms:Sign"]
    assert "TokenSigningKey" in str(stmt["Resource"])


def test_roster_grant_is_update_only(resources):
    """The Lambda writes the roster and must never read or delete it: reads are
    the operator's job, with their own credentials. GetItem would also turn the
    function into a way to enumerate the seller's customer list."""
    (stmt,) = _statements(resources["ActivateFunctionRole"], "RecordActivations")
    assert _actions(stmt) == ["dynamodb:UpdateItem"]
    assert "ActivationsTable" in str(stmt["Resource"])


# ---------------------------------------------------------------------------
# Key + data protection.
# ---------------------------------------------------------------------------


def test_signing_key_is_asymmetric(resources):
    """Asymmetric is what lets a verifier hold only the PUBLIC key. A symmetric
    key would mean shipping the ability to MINT tokens inside a product that runs
    in the customer's own account."""
    props = resources["TokenSigningKey"]["Properties"]
    assert props["KeyUsage"] == "SIGN_VERIFY"
    assert props["KeySpec"].startswith(("RSA_", "ECC_"))


def test_signing_key_policy_does_not_grant_a_wildcard_principal(resources):
    for stmt in resources["TokenSigningKey"]["Properties"]["KeyPolicy"]["Statement"]:
        principal = stmt.get("Principal")
        assert principal != "*", "KMS key policy grants every principal"
        if isinstance(principal, dict):
            assert principal.get("AWS") != "*", (
                "KMS key policy grants every AWS principal"
            )


def test_signing_key_survives_stack_deletion(resources):
    """Destroying the key invalidates every issued token AND makes previously
    issued ones unverifiable — a stack delete must not do that silently."""
    key = resources["TokenSigningKey"]
    assert key["DeletionPolicy"] == "Retain"
    assert key["UpdateReplacePolicy"] == "Retain"


def test_roster_is_encrypted_recoverable_and_retained(resources):
    """The roster is a list of the seller's paying customers by AWS account id."""
    table = resources["ActivationsTable"]
    assert table["Properties"]["SSESpecification"]["SSEEnabled"] is True
    assert (
        table["Properties"]["PointInTimeRecoverySpecification"][
            "PointInTimeRecoveryEnabled"
        ]
        is True
    )
    assert table["DeletionPolicy"] == "Retain"


def test_no_hardcoded_account_ids_or_secrets(template):
    """A committed account id or key would leak the seller's environment; the
    template must stay generic enough for any partner to deploy."""
    raw = _TEMPLATE.read_text(encoding="utf-8")
    import re

    # 12-digit runs that aren't part of an obvious version/port/limit.
    for match in re.finditer(r"\b\d{12}\b", raw):
        pytest.fail(f"possible hardcoded AWS account id: {match.group()}")
    for needle in ("AKIA", "ASIA", "-----BEGIN"):
        assert needle not in raw, f"possible credential material ({needle}) in template"


def test_activation_function_has_reserved_concurrency(resources):
    """A concurrency reservation is a security control here, not just cost.

    The endpoint is reachable by ANY AWS account (SELL.T04), so without a
    reservation a flood consumes the seller account's entire Lambda concurrency
    pool — taking down other seller-side functions — and consumes the shared
    Marketplace SearchAgreements quota at whatever rate Lambda will scale to.
    """
    props = resources["ActivateFunction"]["Properties"]
    assert "ReservedConcurrentExecutions" in props, (
        "no concurrency reservation: abuse of this public endpoint would consume "
        "the whole account's Lambda pool"
    )


def test_api_caching_is_not_enabled(resources):
    """Caching MUST stay off, even though a generic IaC scanner asks for it.

    A cached activation response would keep answering "entitled" after a
    subscription was cancelled — turning a revenue control into a stale one. This
    test exists so the scanner finding is not "fixed" by someone later.
    """
    api_props = resources["ActivationApi"]["Properties"]
    assert api_props.get("CacheClusterEnabled") is not True
    for settings in api_props.get("MethodSettings") or []:
        assert settings.get("CachingEnabled") is not True, (
            "API Gateway caching would serve stale entitlement decisions"
        )


def test_activation_role_has_a_permissions_boundary(resources):
    """Required by organisations whose SCP mandates a boundary on every role.

    Without it, IAM role creation is denied there and the whole stack rolls back —
    the exact defect that previously broke FeaturePlatformStack. Also asserted
    repo-wide by lib/idp_sdk/tests/unit/test_permissions_boundary_coverage.py,
    which this template is now registered in; duplicated here so the failure is
    visible next to the rest of this service's invariants.
    """
    props = resources["ActivateFunctionRole"]["Properties"]
    assert "PermissionsBoundary" in props, (
        "ActivateFunctionRole has no PermissionsBoundary — SCP-enforced accounts "
        "cannot deploy this stack"
    )


def test_log_groups_are_cmk_encrypted(resources):
    """These logs carry buyer AWS account ids and caller role ARNs — customer-
    identifying data for a seller — and CMK-encrypted log groups are the repo
    standard (115 of 135 elsewhere)."""
    for name in (
        "ActivateFunctionLogGroup",
        "ActivationApiAccessLogGroup",
        # Pre-declared precisely so it is encrypted and expires; API Gateway would
        # otherwise auto-create it in plaintext with no retention.
        "ActivationApiExecutionLogGroup",
    ):
        props = resources[name]["Properties"]
        assert "KmsKeyId" in props, f"{name} is not KMS-encrypted"
        assert "LogEncryptionKey" in str(props["KmsKeyId"])


def test_log_key_grants_cloudwatch_logs_narrowly(resources):
    """The grant to the Logs service principal must be scoped by encryption
    context to this account/region, or it is usable from elsewhere."""
    statements = resources["LogEncryptionKey"]["Properties"]["KeyPolicy"]["Statement"]
    logs_stmts = [
        st
        for st in statements
        if isinstance(st.get("Principal"), dict)
        and "logs." in str(st["Principal"].get("Service", ""))
    ]
    assert logs_stmts, "no statement grants CloudWatch Logs use of the key"
    for st in logs_stmts:
        condition = st.get("Condition") or {}
        assert condition, "CloudWatch Logs grant has no Condition — unscoped"
        assert "kms:EncryptionContext:aws:logs:arn" in str(condition), (
            "grant is not scoped by log-group encryption context"
        )


def test_account_cloudwatch_role_is_present_and_retained(resources):
    """Without the account-level API Gateway CloudWatch role, a REST stage with
    AccessLogSetting is rejected outright — "CloudWatch Logs role ARN must be set
    in account settings" — so a fresh seller account cannot deploy this template
    at all. It must also be Retain: AWS::ApiGateway::Account is an account/region
    singleton, and deleting this stack would otherwise clear the setting out from
    under any other API in the seller's account."""
    account = resources["ApiGatewayAccount"]
    assert account["Type"] == "AWS::ApiGateway::Account"
    assert account["DeletionPolicy"] == "Retain", (
        "deleting this stack would disable access logging for every other API "
        "Gateway API in the seller's account"
    )
    role = resources["ApiGatewayCloudWatchRole"]
    assert role["DeletionPolicy"] == "Retain"
    assert "PermissionsBoundary" in role["Properties"]
    assert "AmazonAPIGatewayPushToCloudWatchLogs" in str(
        role["Properties"]["ManagedPolicyArns"]
    )


def test_kms_key_policies_use_no_overly_broad_wildcard_actions(resources):
    """Trailing-wildcard KMS actions grant more than they appear to.

    `kms:Decrypt*` / `kms:Encrypt*` / `kms:Describe*` read as if they mean the one
    obvious verb, but they also match every current and FUTURE action with that
    prefix — `kms:DescribeCustomKeyStores`, for instance. Only `GenerateDataKey*`
    and `ReEncrypt*` need to be wildcards (they have real variant forms that
    CloudWatch Logs uses).

    A sole `kms:*` for the account root is exempt: AWS explicitly warns against
    key policies that do not grant root full access, because it can permanently
    orphan the key.
    """
    allowed_wildcards = {
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
        "kms:GenerateDataKeyPair*",
    }
    for name in ("TokenSigningKey", "LogEncryptionKey"):
        for stmt in resources[name]["Properties"]["KeyPolicy"]["Statement"]:
            if stmt.get("Effect") != "Allow":
                continue
            actions = _actions(stmt)
            principal = stmt.get("Principal") or {}
            is_root = "root" in str(principal.get("AWS", ""))
            if is_root and actions == ["kms:*"]:
                continue
            for action in actions:
                assert "*" not in action or action in allowed_wildcards, (
                    f"{name} statement {stmt.get('Sid')!r} grants {action!r} — a "
                    "trailing wildcard that is broader than intended; name the "
                    "exact action instead"
                )


def test_log_key_is_separate_from_the_signing_key(resources):
    """The signing key is SIGN_VERIFY and cannot encrypt; conflating them would
    also widen what a log-encryption grant reaches."""
    assert resources["LogEncryptionKey"]["Properties"]["KeyUsage"] == "ENCRYPT_DECRYPT"
    assert resources["TokenSigningKey"]["Properties"]["KeyUsage"] == "SIGN_VERIFY"


def test_log_key_is_not_retained(resources):
    """Unlike the signing key, retaining this would orphan a key on every test
    teardown while only protecting already-expiring log data."""
    assert resources["LogEncryptionKey"].get("DeletionPolicy") != "Retain"
