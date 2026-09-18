# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Repo-wide gate on CloudWatch log-group encryption at rest.

``HttpApiDispatcherLogGroup`` in ``nested/api-resolvers/template.yaml`` shipped
with no ``KmsKeyId`` while 106 of its 109 siblings across the three main-deployment
templates had one. Nothing caught it, because encryption on these log groups was a
**convention** — 106 resources that happened to carry the property — and a
convention that is never consulted at the point of decision is not a control. This
module is that missing check.

The same commit also gave that group ``RetentionInDays: !Ref LogRetentionDays`` in
place of a hardcoded ``30``. It was the only one of the 109 that hardcoded
retention, so rule 2 below needs **no exemptions at all** in the enforced set: an
operator who lowers ``LogRetentionDays`` to shrink a bill, or raises it to satisfy a
records-retention rule, now moves all 109 together.

Rules enforced on ``CMK_TEMPLATES``
-----------------------------------
1. Every ``AWS::Logs::LogGroup`` sets ``KmsKeyId``, unless it is listed in
   ``CUSTOM_RESOURCE_ONLY_EXEMPT`` with a justification.
2. Every ``AWS::Logs::LogGroup`` takes ``RetentionInDays`` from a parameter rather
   than hardcoding a literal. No exemptions.

Both rules resolve ``Fn::If`` branches and treat ``AWS::NoValue`` as absent, in
either the short (``!Ref``) or long (``Ref:``) spelling. A property that is
*syntactically present* but resolves to nothing on some branch is the obvious way
to defeat a check like this, and it is the way the sibling gate
(``test_lambda_log_groups.py``) was actually defeated before it was hardened, so
``test_rule_1_catches_known_bypasses`` and ``test_rule_2_catches_hardcoded_retention``
pin each shape closed. They earned their keep immediately: the long-form case caught
a real bug in this module's own first draft.

Scope, and what is deliberately outside it
------------------------------------------
Rules 1 and 2 are enforced only on ``CMK_TEMPLATES`` — the three templates of the
main deployment. Those three either own the customer-managed key or receive its ARN
as a parameter, so every log group in them *can* be encrypted with it, which is why
the convention was already at 106/109 there.

The other templates the sibling gate covers (installable features, customer-
deployable sample hook stacks, one throwaway verification fixture) are separate
stacks, and encrypting their log groups is a real change to each — a new parameter
or a new key, not one property. Those are **not** silently excluded: rule 3
(``test_no_new_unencrypted_groups_outside_the_cmk_templates``) pins the known set so
the debt is visible and cannot grow, and each template carries its reason in
``OUTSIDE_CMK_TEMPLATES``. Without rule 3 this module would gate 3 of 22 templates
while reading, from the outside, as though log-group encryption were gated
everywhere — which is the same "control that exists but is not consulted" failure it
was written to close.

Key policy
----------
No key-policy change is needed for anything rule 1 requires in these three
templates. ``CustomerManagedEncryptionKey`` in ``template.yaml`` grants
``logs.${AWS::URLSuffix}`` the encrypt/decrypt/describe set on ``Resource: "*"``
with **no** ``Condition``, so the grant is not scoped by
``kms:EncryptionContext:aws:logs:arn`` to a particular set of log groups. If that
statement ever gains a condition, adding ``KmsKeyId`` to a new group stops being a
one-property change and this docstring is wrong —
``test_cloudwatch_logs_key_grant_is_not_scoped_to_named_groups`` fails in that case
rather than letting the assumption rot.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The three templates of the main deployment. `template.yaml` declares
# CustomerManagedEncryptionKey; the other two receive its ARN as the
# `CustomerManagedEncryptionKeyArn` parameter.
CMK_TEMPLATES = [
    "template.yaml",
    "patterns/unified/template.yaml",
    "nested/api-resolvers/template.yaml",
]

# Log groups exempt from rule 1. Both belong to Lambdas that run ONLY during a
# CloudFormation stack operation, and both already carry the matching cfn_nag W84
# suppression and checkov CKV_AWS_158 skip in the template itself.
#
# Why an unencrypted group is acceptable for this class: the function is invoked a
# handful of times per stack operation and logs nothing but the progress of that
# operation — a stack-name length check, and a read of the previously deployed
# pattern from SSM. There is no document content, no request payload and no user
# identity in either. The repo treats this class the same way for retention
# (`test_lambda_log_groups.py`'s CUSTOM_RESOURCE_ONLY), where an auto-created group
# with indefinite retention is an accepted cost for the same reason.
#
# Adding an entry here is a decision, not a formality: confirm by hand that nothing
# invokes the function outside a stack operation, and that it cannot log document or
# request data. `test_exemptions_still_exist_and_are_log_groups` only checks that an
# entry is not stale.
CUSTOM_RESOURCE_ONLY_EXEMPT: dict[str, set[str]] = {
    "template.yaml": {
        # Custom::StacknameCheck handler. Logs the stack name length verdict.
        "StacknameCheckFunctionLogGroup",
        # Custom::ReadPreviousIDPPattern handler. Logs one SSM parameter read.
        "ReadPreviousIDPPatternFunctionLogGroup",
    },
}

# Templates outside the enforced set, with the log groups in each that carry no
# KmsKeyId today. Rule 3 pins this exactly: a new unencrypted group in any of them
# fails, and so does encrypting one without updating the entry here.
#
# These are separate stacks. Encrypting them means giving each a key parameter (or a
# key), plus the key-policy and IAM work that follows, per stack — worth doing, but
# not one property each, so it is tracked separately rather than folded in here.
OUTSIDE_CMK_TEMPLATES: dict[str, tuple[str, set[str]]] = {
    # Installable features. Each imports the main stack's key ARN already, for IAM
    # statements, so wiring it into these log groups is the smallest of the group
    # below — but it is still a change to each feature stack.
    "feature-platform/confbench-testset/template.yaml": (
        "installable feature stack; imports the main stack's key ARN for IAM but "
        "does not yet pass it to its log groups",
        {
            "FailFunctionLogGroup",
            "FeatureApiFunctionLogGroup",
            "FinalizeFunctionLogGroup",
            "IngestStateMachineLogGroup",
            "IngestWorkerFunctionLogGroup",
            "PlannerFunctionLogGroup",
            "UiDeployerFunctionLogGroup",
        },
    ),
    "feature-platform/pii-anonymizer/template.yaml": (
        "installable feature stack; as above",
        {
            "FeatureApiFunctionLogGroup",
            "PiiAnonymizerHookFunctionLogGroup",
            "UiDeployerFunctionLogGroup",
        },
    ),
    "feature-platform/sample-feature/template.yaml": (
        "installable feature stack; as above",
        {"FeatureApiFunctionLogGroup", "UiDeployerFunctionLogGroup"},
    ),
    "feature-platform/sample-health-insurance-review/template.yaml": (
        "installable feature stack; as above",
        {
            "ClaimStatusHookFunctionLogGroup",
            "FeatureApiFunctionLogGroup",
            "UiDeployerFunctionLogGroup",
        },
    ),
    # Scaffolding a developer copies to start a feature. Takes no key parameter at
    # all, by design — it has to deploy standalone.
    "feature-platform/feature-template/template.yaml": (
        "copy-me scaffolding for a new feature; takes no key parameter by design",
        {"FeatureApiFunctionLogGroup", "UiDeployerFunctionLogGroup"},
    ),
    # Customer-deployable sample hook stacks. Each of these groups already carries
    # an in-template comment saying it is deliberately not CMK-encrypted and telling
    # the reader to add a parameter and a KmsKeyId if their account requires it.
    "samples/lambda-hook-inference/template.yaml": (
        "customer-deployable sample; deliberate, and stated per group in-template",
        {
            "BedrockProxyFunctionLogGroup",
            "ChandraOcrHookFunctionLogGroup",
            "CohereParseHookFunctionLogGroup",
            "MistralOcrHookFunctionLogGroup",
            "SageMakerHookFunctionLogGroup",
        },
    ),
    "samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency/template.yaml": (
        "customer-deployable sample; as above",
        {"W2CopyConsistencyFunctionLogGroup"},
    ),
    # Throwaway fixture for `make verify-idp-federation`: created and deleted inside
    # one verification run, retention 1 day, logs a synthetic OIDC handshake.
    "scripts/security/live_checks/oidc_provider/template.yaml": (
        "throwaway verification fixture, deleted at end of run, 1-day retention",
        {"IdpFunctionLogGroup"},
    ),
}


# ---------------------------------------------------------------------------
# Template loading
#
# The CFN-tolerant YAML loader lives in one place in this repo. Load it BY PATH
# rather than importing `idp_sdk`: an editable install can resolve `idp_sdk` to a
# different checkout entirely (it does on the maintainer's cloud desktop, where a
# second worktree shadows the first), which would silently gate whichever tree pip
# happened to point at. `spec_from_file_location` is the pattern the sibling tests
# in this directory already use, and it also sidesteps ruff's TID251 ban on
# importing `idp_sdk._core` directly.
# ---------------------------------------------------------------------------
_CFN_YAML = REPO_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "cfn_yaml.py"


def _cfn_yaml_module() -> Any:
    spec = importlib.util.spec_from_file_location("_cfn_yaml_for_gate", _CFN_YAML)
    assert spec is not None and spec.loader is not None, f"cannot load {_CFN_YAML}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(rel_path: str) -> dict:
    return _cfn_yaml_module().load_cfn_template(REPO_ROOT / rel_path)


def _load_text(text: str) -> dict:
    return _cfn_yaml_module().load_cfn_yaml(text) or {}


def _log_groups(doc: dict) -> dict[str, dict]:
    return {
        lid: body
        for lid, body in (doc.get("Resources") or {}).items()
        if body.get("Type") == "AWS::Logs::LogGroup"
    }


# ---------------------------------------------------------------------------
# Intrinsic handling
#
# The loader keeps short-form intrinsics as {"!Tag": value}; a template may also
# spell the same thing long-form ({"Fn::If": ...}). Every helper below accepts both,
# because accepting only one is how a gate ends up passing on a template that
# defeats it.
# ---------------------------------------------------------------------------
def _intrinsic(node: Any, name: str) -> Any:
    """Return the argument of intrinsic `name` on `node`, or None.

    `Ref` is the one intrinsic CloudFormation spells *without* the `Fn::` prefix in
    long form — `{"Ref": "AWS::NoValue"}`, not `{"Fn::Ref": ...}`. Missing that is
    not hypothetical: it is the bug `test_rule_1_catches_known_bypasses`'s long-form
    case caught in the first draft of this module, where a `KmsKeyId` whose
    `Fn::If` false branch was a long-form `Ref: AWS::NoValue` read as encrypted.
    """
    if not isinstance(node, dict):
        return None
    keys = (f"!{name}", f"Fn::{name}")
    if name == "Ref":
        keys += ("Ref",)
    for key in keys:
        if key in node:
            return node[key]
    return None


def _is_no_value(node: Any) -> bool:
    return _intrinsic(node, "Ref") == "AWS::NoValue"


def _branches(node: Any) -> list[Any]:
    """Every value `node` can resolve to, flattening nested `Fn::If`."""
    args = _intrinsic(node, "If")
    if isinstance(args, list) and len(args) == 3:
        return _branches(args[1]) + _branches(args[2])
    return [node]


def _is_absent(value: Any) -> bool:
    """True if `value` contributes nothing on at least one branch.

    A property missing outright, set to null, set to `AWS::NoValue`, or set to the
    empty string is absent. So is one whose `Fn::If` has an absent branch: it would
    deploy unencrypted under that condition, which is exactly the case a
    presence-only check waves through.
    """
    return any(
        branch is None or branch == "" or _is_no_value(branch)
        for branch in _branches(value)
    )


def _hardcodes_retention(value: Any) -> bool:
    """True if any branch of `RetentionInDays` is a literal rather than a `Ref`."""
    for branch in _branches(value):
        if branch is None or _is_no_value(branch):
            # Absent retention is the sibling gate's rule 2, not this one. Not
            # re-asserted here, so a change there cannot be masked by a pass here.
            continue
        if not isinstance(branch, dict):
            return True
    return False


def _unencrypted(doc: dict, exempt: set[str]) -> list[str]:
    return sorted(
        lid
        for lid, body in _log_groups(doc).items()
        if lid not in exempt
        and _is_absent((body.get("Properties") or {}).get("KmsKeyId"))
    )


def _hardcoded_retention(doc: dict) -> list[str]:
    return sorted(
        lid
        for lid, body in _log_groups(doc).items()
        if _hardcodes_retention((body.get("Properties") or {}).get("RetentionInDays"))
    )


# ---------------------------------------------------------------------------
# Rule 1 — encryption at rest
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("rel_path", CMK_TEMPLATES)
def test_every_log_group_is_encrypted_with_the_customer_managed_key(
    rel_path: str,
) -> None:
    """A log group with no `KmsKeyId` is encrypted with the AWS-owned key instead.

    The customer-managed key is what gives an operator a single place to revoke,
    rotate and audit access to log data across the deployment, so a group that
    misses it is outside every control expressed through that key.
    """
    exempt = CUSTOM_RESOURCE_ONLY_EXEMPT.get(rel_path, set())
    offenders = _unencrypted(_load(rel_path), exempt)

    assert not offenders, (
        f"{rel_path}: log group(s) with no KmsKeyId: {offenders}. "
        f"Set `KmsKeyId: !Ref CustomerManagedEncryptionKeyArn` (nested templates) or "
        f"`KmsKeyId: !GetAtt CustomerManagedEncryptionKey.Arn` (template.yaml). The "
        f"key policy already grants logs.${{AWS::URLSuffix}} unconditionally, so no "
        f"policy change is needed. If the group belongs to a Lambda that runs only "
        f"during a stack operation, add it to CUSTOM_RESOURCE_ONLY_EXEMPT with a "
        f"justification instead."
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", sorted(CUSTOM_RESOURCE_ONLY_EXEMPT))
def test_exemptions_still_exist_and_are_log_groups(rel_path: str) -> None:
    """A stale exemption silently widens the rule it was carved out of."""
    groups = _log_groups(_load(rel_path))
    for lid in sorted(CUSTOM_RESOURCE_ONLY_EXEMPT[rel_path]):
        assert lid in groups, (
            f"{rel_path}: exempt resource {lid!r} is not an AWS::Logs::LogGroup in "
            f"this template (renamed or removed?). Drop the exemption."
        )


# ---------------------------------------------------------------------------
# Rule 2 — retention comes from a parameter
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("rel_path", CMK_TEMPLATES)
def test_every_log_group_takes_retention_from_a_parameter(rel_path: str) -> None:
    """One group hardcoding retention makes `LogRetentionDays` a partial control.

    Deliberately has no exemption list. Both rule-1 exemptions parameterise
    retention already, so nothing in these three templates needs one, and an empty
    exemption list is a stronger statement than an unused one.
    """
    offenders = _hardcoded_retention(_load(rel_path))

    assert not offenders, (
        f"{rel_path}: log group(s) hardcoding RetentionInDays: {offenders}. "
        f"Use `RetentionInDays: !Ref LogRetentionDays` so an operator changing the "
        f"stack parameter moves every log group together."
    )


# ---------------------------------------------------------------------------
# Rule 3 — the templates outside the enforced set do not drift
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("rel_path", sorted(OUTSIDE_CMK_TEMPLATES))
def test_no_new_unencrypted_groups_outside_the_cmk_templates(rel_path: str) -> None:
    """Pin the known gap so it is visible and cannot grow.

    Fails in both directions on purpose. A new unencrypted group in one of these
    stacks is new debt and needs a decision; encrypting one is progress and should
    shrink this list rather than leave a name behind that reads as an accepted gap.
    """
    reason, expected = OUTSIDE_CMK_TEMPLATES[rel_path]
    actual = set(_unencrypted(_load(rel_path), set()))

    assert actual == expected, (
        f"{rel_path}: the set of log groups with no KmsKeyId changed.\n"
        f"  recorded: {sorted(expected)}\n"
        f"  actual:   {sorted(actual)}\n"
        f"  new:      {sorted(actual - expected)}\n"
        f"  fixed:    {sorted(expected - actual)}\n"
        f"Recorded reason for this template: {reason}.\n"
        f"If you added a log group here, prefer encrypting it with the stack's key. "
        f"If that is genuinely not available, update OUTSIDE_CMK_TEMPLATES and say "
        f"why. If you encrypted one, remove it from the recorded set."
    )


@pytest.mark.unit
def test_enforced_and_pinned_template_sets_do_not_overlap() -> None:
    """A template in both sets would be enforced and excused at the same time."""
    overlap = set(CMK_TEMPLATES) & set(OUTSIDE_CMK_TEMPLATES)
    assert not overlap, f"template(s) both enforced and pinned as a gap: {overlap}"


# ---------------------------------------------------------------------------
# Key-policy assumption
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_cloudwatch_logs_key_grant_is_not_scoped_to_named_groups() -> None:
    """Rule 1's remedy is one property only while this grant stays unconditional.

    If the CloudWatch Logs statement gains a `Condition` (an
    `kms:EncryptionContext:aws:logs:arn` scope is the usual one), then adding
    `KmsKeyId` to a group not covered by that scope produces a group CloudWatch
    cannot write to — a deploy-time or runtime failure, not a lint error. Better to
    fail here, next to the advice, than to leave the advice quietly wrong.
    """
    doc = _load("template.yaml")
    key = (doc.get("Resources") or {}).get("CustomerManagedEncryptionKey")
    assert key is not None, "CustomerManagedEncryptionKey is gone; revisit this gate"

    policy = (key.get("Properties") or {}).get("KeyPolicy") or {}
    matching = [
        stmt
        for stmt in (policy.get("Statement") or [])
        if isinstance(stmt, dict)
        and "logs." in str((stmt.get("Principal") or {}).get("Service", ""))
    ]
    assert matching, "no key-policy statement grants CloudWatch Logs use of the key"

    for stmt in matching:
        assert "Condition" not in stmt, (
            f"the CloudWatch Logs key grant {stmt.get('Sid')!r} is now conditional. "
            f"Adding KmsKeyId to a log group is no longer a one-property change — "
            f"check the new condition covers every group this gate requires it on, "
            f"then update this test and the module docstring."
        )


# ---------------------------------------------------------------------------
# The gate's own regression tests
#
# A guard that passes on a broken tree is worthless, and the way that happens is
# that the guard is only ever run against a tree that is already clean. These feed
# it synthetic templates whose defect is known.
# ---------------------------------------------------------------------------
_UNENCRYPTED = """
Resources:
  ProbeLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: !Ref LogRetentionDays
"""

_NO_VALUE_KEY = """
Resources:
  ProbeLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      KmsKeyId: !Ref AWS::NoValue
      RetentionInDays: !Ref LogRetentionDays
"""

_EMPTY_KEY = """
Resources:
  ProbeLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      KmsKeyId: ""
      RetentionInDays: !Ref LogRetentionDays
"""

_CONDITIONAL_KEY = """
Resources:
  ProbeLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      KmsKeyId: !If [SomeCondition, !Ref CustomerManagedEncryptionKeyArn, !Ref "AWS::NoValue"]
      RetentionInDays: !Ref LogRetentionDays
"""

_LONG_FORM_CONDITIONAL_KEY = """
Resources:
  ProbeLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      KmsKeyId:
        Fn::If:
          - SomeCondition
          - Ref: CustomerManagedEncryptionKeyArn
          - Ref: AWS::NoValue
      RetentionInDays: !Ref LogRetentionDays
"""


@pytest.mark.unit
@pytest.mark.parametrize(
    "case,body",
    [
        ("no KmsKeyId at all", _UNENCRYPTED),
        ("KmsKeyId: !Ref AWS::NoValue", _NO_VALUE_KEY),
        ("KmsKeyId: empty string", _EMPTY_KEY),
        ("KmsKeyId only on one Fn::If branch", _CONDITIONAL_KEY),
        ("same, long-form Fn::If/Ref", _LONG_FORM_CONDITIONAL_KEY),
    ],
)
def test_rule_1_catches_known_bypasses(case: str, body: str) -> None:
    """Each of these was a way to look encrypted without being encrypted."""
    assert _unencrypted(_load_text(body), set()) == ["ProbeLogGroup"], (
        f"rule 1 failed to flag a log group that is not CMK-encrypted: {case}"
    )


@pytest.mark.unit
def test_rule_1_accepts_a_properly_encrypted_group() -> None:
    """The complement of the cases above: the gate must not flag a correct group."""
    body = """
Resources:
  GoodLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      KmsKeyId: !Ref CustomerManagedEncryptionKeyArn
      RetentionInDays: !Ref LogRetentionDays
"""
    doc = _load_text(body)
    assert _unencrypted(doc, set()) == []
    assert _hardcoded_retention(doc) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "case,value",
    [
        ("integer literal", "30"),
        ("quoted literal", '"30"'),
        ("literal on one Fn::If branch", "!If [C, !Ref LogRetentionDays, 30]"),
    ],
)
def test_rule_2_catches_hardcoded_retention(case: str, value: str) -> None:
    body = f"""
Resources:
  ProbeLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      KmsKeyId: !Ref CustomerManagedEncryptionKeyArn
      RetentionInDays: {value}
"""
    assert _hardcoded_retention(_load_text(body)) == ["ProbeLogGroup"], (
        f"rule 2 failed to flag hardcoded retention: {case}"
    )


@pytest.mark.unit
def test_the_gate_reads_this_worktree() -> None:
    """Guard against gating a different checkout than the one under test.

    `_cfn_yaml_module` loads the loader by path for this reason; this asserts the
    path actually resolved inside this tree, so the failure mode is a clear message
    rather than a gate that quietly parsed somebody else's templates.
    """
    module = _cfn_yaml_module()
    assert Path(module.__file__ or "").resolve().is_relative_to(REPO_ROOT), (
        f"CFN loader resolved to {module.__file__}, outside {REPO_ROOT}"
    )
    for rel_path in CMK_TEMPLATES:
        assert (REPO_ROOT / rel_path).is_file(), f"{rel_path} missing from worktree"


@pytest.mark.unit
def test_the_enforced_set_is_not_vacuous() -> None:
    """A loader change that returned {} would make every rule above pass silently.

    The count is asserted as a floor, not an equality: this gate should not need
    editing every time a Lambda is added. It only has to be impossible for the
    enforced set to collapse to nothing.
    """
    total = sum(len(_log_groups(_load(rel))) for rel in CMK_TEMPLATES)
    assert total >= 100, (
        f"expected the three main templates to declare at least 100 log groups, "
        f"found {total} — the loader or the template list is probably broken, and "
        f"every assertion in this module would pass vacuously."
    )
