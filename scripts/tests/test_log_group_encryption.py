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
records-retention rule, now moves every one of them together.

Every ``AWS::Logs::LogGroup`` in the repository is in exactly one of three
categories, and ``test_every_log_group_template_is_categorised`` fails if a template
declaring one is in none of them. That meta-test is the whole reason the categories
can be trusted: the first revision of this module pinned a *hand-built* inventory of
the wider class, and the inventory disagreed with this module's own ``_is_absent``
predicate by eleven log groups across six templates that no category mentioned at
all (see "How the categories are drawn" below).

Rules
-----
On ``CMK_TEMPLATES`` — the templates that own the customer-managed key or take its
ARN as a **mandatory** parameter:

1. Every ``AWS::Logs::LogGroup`` sets ``KmsKeyId`` unconditionally, unless it is
   listed in ``CUSTOM_RESOURCE_ONLY_EXEMPT``.
2. Every ``AWS::Logs::LogGroup`` takes ``RetentionInDays`` from ``Ref`` of a
   parameter in ``RETENTION_PARAMETERS``. No exemptions.

On ``OUTSIDE_CMK_TEMPLATES`` — templates that cannot reach a key without new
wiring:

3. The set of log groups lacking ``KmsKeyId`` matches the recorded set exactly, in
   both directions.

On ``OPTIONAL_KEY_TEMPLATES`` — **derived**, not listed; see below:

4. Every log group's ``KmsKeyId`` references ``CustomerManagedEncryptionKeyArn`` on
   the branch where the key is present, and nothing else.
5. Where ``template.yaml`` instantiates such a template as a nested stack, it passes
   that parameter unconditionally — otherwise the category is a promise nothing
   checks.

Rules 1 and 4 resolve ``Fn::If`` branches and treat ``AWS::NoValue`` as absent, in
either the short (``!Ref``) or long (``Ref:``) spelling. A property that is
*syntactically present* but resolves to nothing on some branch is the obvious way to
defeat a check like this, and it is the way the sibling gate
(``test_lambda_log_groups.py``) was actually defeated before it was hardened, so
``test_rule_1_catches_known_bypasses`` and ``test_rule_2_catches_hardcoded_retention``
pin each shape closed. They earned their keep immediately: the long-form case caught
a real bug in this module's own first draft, and the ``!Sub "30"`` case caught rule 2
accepting a literal thirty wrapped in an intrinsic.

How the categories are drawn
----------------------------
Membership follows a **structural** property of each template, not a name list,
because the first revision's hand-built list was wrong in a way a name list cannot
protect against.

A template that declares ``CustomerManagedEncryptionKeyArn`` with ``Default: ''``
has an *optional* key, and the only correct way to write a log group there is the
conditional form::

    KmsKeyId: !If [HasKey, !Ref CustomerManagedEncryptionKeyArn, !Ref 'AWS::NoValue']

``_is_absent`` counts that as absent — deliberately, because a ``NoValue`` branch is
exactly how a resource looks encrypted without being encrypted — while a naive "is
the property present?" audit counts it as encrypted. The first revision's inventory
was built with the naive predicate, so it recorded 24 log groups across 8 templates
where this module's predicate finds 35 across 14. The eleven-group difference is
entirely these conditional writes, and it was invisible because the eight templates
the inventory *did* pin happen to omit ``KmsKeyId`` outright, where both predicates
agree.

None of those eleven is a live exposure. Six are in
``feature-platform/main-stack-extensions/template.yaml``, whose only deployment is
as a nested stack of ``template.yaml``, which passes
``CustomerManagedEncryptionKeyArn: !GetAtt CustomerManagedEncryptionKey.Arn`` from a
key resource carrying no ``Condition`` — so the condition is true in every real
deployment, and the ``Default: ''`` is unreachable defensive code for a hypothetical
standalone nested deploy. Rule 5 is what keeps that true. The other five are the
``samples/lambda-hook-inference/GENAIIDP-*`` stacks, which customers deploy
standalone, where an optional key is the correct design outright.

Simplifying those six resources to an unconditional ``!Ref`` would let
``main-stack-extensions`` join ``CMK_TEMPLATES`` honestly, and was considered. It was
rejected: it is a functional template change (an empty ``KmsKeyId`` is rejected by
CloudFormation, so it converts unreachable defensive code into a hard failure on the
standalone path), and it would fix only that one template — the five sample hooks
would still need a category, and their conditional form is not a defect to fix. One
derived predicate covers all six, and pairing it with rule 5 proves the property
that actually matters operationally, which the unconditional rewrite would only have
assumed.

Key policy
----------
No key-policy change is needed for anything rule 1 requires.
``CustomerManagedEncryptionKey`` in ``template.yaml`` grants ``logs.${AWS::URLSuffix}``
the encrypt/decrypt/describe set on ``Resource: "*"`` with no ``Condition``.
``test_cloudwatch_logs_key_grant_covers_every_log_group_name_shape`` does **not**
require it to stay unscoped — see that test for why a per-group scope would break the
deployment but an account-and-region scope would not.
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from repo_files import tracked_paths

REPO_ROOT = Path(__file__).resolve().parents[2]

LOG_GROUP_TYPE = "AWS::Logs::LogGroup"

# The parameter through which a nested stack receives the main stack's key ARN.
KEY_PARAMETER = "CustomerManagedEncryptionKeyArn"

# Parameters rule 2 accepts as the source of `RetentionInDays`. An allowlist, not
# "any parameter": a group pinned to some *other* parameter still defeats the point
# of the rule, which is that one operator action moves every group together.
RETENTION_PARAMETERS = {"LogRetentionDays"}

# Templates that own the customer-managed key, or take its ARN as a MANDATORY
# parameter (`Type: String` with no `Default`). Rules 1 and 2 are enforced here, and
# the unconditional form is required, because the key cannot be absent.
#
# `nested/multi-doc-discovery/template.yaml` belongs here for exactly that reason and
# was missing from the first revision: it is an unconditional nested stack of
# `template.yaml`, takes the ARN as a mandatory parameter, and already sets
# `KmsKeyId` and `RetentionInDays` correctly on all five of its log groups. Nothing
# gated that.
CMK_TEMPLATES = [
    "template.yaml",
    "patterns/unified/template.yaml",
    "nested/api-resolvers/template.yaml",
    "nested/multi-doc-discovery/template.yaml",
]

# Log groups exempt from rule 1. Both belong to Lambdas that run ONLY during a
# CloudFormation stack operation.
#
# Why an unencrypted group is acceptable for this class: the function is invoked a
# handful of times per stack operation, and what it logs is the progress of that
# operation rather than anything flowing through the deployment — no document
# content, no end-user request payload, no end-user identity. The repo treats this
# class the same way for retention (`test_lambda_log_groups.py`'s
# CUSTOM_RESOURCE_ONLY), where an auto-created group with indefinite retention is an
# accepted cost for the same reason.
#
# Adding an entry here is a decision, not a formality, and it is checked rather than
# trusted: `test_exemptions_are_evidenced_and_custom_resource_only` requires the log
# group to carry a cfn_nag `W84` suppression with a written reason, and re-derives
# the custom-resource-only property from the template through the sibling gate's
# structural predicate.
CUSTOM_RESOURCE_ONLY_EXEMPT: dict[str, set[str]] = {
    "template.yaml": {
        # Custom::StacknameCheck handler. Logs the stack name length verdict.
        "StacknameCheckFunctionLogGroup",
        # Custom::ReadPreviousIDPPattern handler. Logs one SSM parameter read, and
        # `json.dumps(event)` of the custom-resource event itself, which includes
        # the presigned `ResponseURL` — a short-lived, single-use CloudFormation
        # callback URL. That is more than "one SSM read", but it is still confined
        # to one stack operation and contains no document or end-user data, so the
        # exemption stands. See src/lambda/read_previous_idp_pattern/index.py.
        "ReadPreviousIDPPatternFunctionLogGroup",
    },
}

# Templates outside the enforced set, with the log groups in each that carry no
# KmsKeyId today. Rule 3 pins this exactly: a new unencrypted group in any of them
# fails, and so does encrypting one without updating the entry here.
#
# An entry with an EMPTY set is not a mistake and is the stronger statement: the
# template is outside the enforced set — its retention or its key does not follow the
# main stack's parameters, so rules 1 and 2 do not apply — but every log group in it
# is encrypted today and rule 3 fails if that regresses.
OUTSIDE_CMK_TEMPLATES: dict[str, tuple[str, set[str]]] = {
    # Installable features. Each already imports the main stack's key ARN for IAM
    # statements. Note that `feature-platform/idp-data-generator/template.yaml`
    # below encrypts its log groups through that same
    # `Fn::ImportValue '${MainStackName}-CustomerManagedEncryptionKeyArn'` with no
    # new parameter at all, so closing these is cheaper than "a parameter each".
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
    # --- Encrypted today, but not through the main stack's parameters, so rules 1
    # --- and 2 do not describe them. Pinned at empty so a regression still fails.
    "feature-platform/idp-data-generator/template.yaml": (
        "installable feature stack; all 5 log groups encrypted via "
        "Fn::ImportValue of the main stack's key ARN, not a parameter",
        set(),
    ),
    "feature-platform/seller-entitlement-service/template.yaml": (
        "installable feature stack; all 3 log groups encrypted with its own "
        "LogEncryptionKey, and retention comes from LogRetentionInDays",
        set(),
    ),
    "notebooks/examples/demo-lambda/template.yml": (
        "standalone notebook example; its one log group takes either a key it "
        "creates or a key ARN passed in, and hardcodes 7-day retention",
        set(),
    ),
}


# ---------------------------------------------------------------------------
# Template loading
#
# The CFN-tolerant YAML loader lives in one place in this repo. Load it BY PATH
# rather than importing `idp_sdk`, for three reasons, weakest first.
#
# An editable install can resolve `idp_sdk` to a different checkout entirely (it
# does on the maintainer's cloud desktop, where a second worktree shadows the
# first). That would not gate the wrong templates — those are always read from
# `REPO_ROOT / rel_path` — but it would parse this tree's templates with a
# possibly-divergent parser from another tree.
#
# It also sidesteps ruff's TID251 ban on importing `idp_sdk._core` directly.
#
# The real reason is the strongest: a repository gate should not require an
# editable install to be present at all, so that `pytest scripts/tests` works in a
# bare checkout. Adding a public re-export of `load_cfn_template` to `idp_sdk` would
# NOT have solved this — `from idp_sdk import load_cfn_template` still resolves
# through whatever is installed, or fails when nothing is.
#
# `spec_from_file_location` is the mechanism three sibling tests in this directory
# already use for other scripts (`test_check_data_plane_tags.py`,
# `test_data_plane_component_labels.py`, `test_ux_recorder.py`), but none of them
# use it for the CloudFormation loader. This file is the FIRST here to reuse the
# canonical loader at all; the eight other modules in this directory that parse
# CloudFormation each still roll their own `_CfnLoader`/`_CfnTagLoader` (counted by
# grepping for those class definitions, and including the sibling gate imported
# below), and converting them is a follow-up rather than part of this change.
# ---------------------------------------------------------------------------
_CFN_YAML = REPO_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "cfn_yaml.py"

# The sibling log-group gate. Imported for `custom_resource_only_violation` (the
# structural proof that a Lambda runs only during a stack operation) rather than
# copied: a third copy of that is exactly how two justifications drift apart. File
# discovery is NOT taken from it — both gates now share `repo_files.tracked_paths`.
_SIBLING_GATE = REPO_ROOT / "scripts" / "tests" / "test_lambda_log_groups.py"


def _module_from_path(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=None)
def _cfn_yaml_module() -> Any:
    return _module_from_path("_cfn_yaml_for_gate", _CFN_YAML)


@functools.lru_cache(maxsize=None)
def _sibling_module() -> Any:
    return _module_from_path("_lambda_log_groups_for_gate", _SIBLING_GATE)


def _load_at(root: Path, rel_path: str) -> dict:
    return _cfn_yaml_module().load_cfn_template(root / rel_path)


def _load(rel_path: str) -> dict:
    return _load_at(REPO_ROOT, rel_path)


def _load_text(text: str) -> dict:
    return _cfn_yaml_module().load_cfn_yaml(text) or {}


def _log_groups(doc: dict) -> dict[str, dict]:
    return {
        lid: body
        for lid, body in (doc.get("Resources") or {}).items()
        if isinstance(body, dict) and body.get("Type") == LOG_GROUP_TYPE
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

    `Fn::Ref` is also accepted, which is not valid CloudFormation. Over-acceptance
    here is harmless — cfn-lint rejects the spelling long before this gate runs —
    and it keeps the helper symmetric with the sibling gate, which does the same.
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
    """Every value `node` can resolve to, flattening nested `Fn::If`.

    A malformed `Fn::If` — a list of any length but three — falls through to
    `[node]` and is therefore treated as a present value rather than analysed.
    cfn-lint catches malformed `Fn::If` as a separate error class, so the gap
    cannot reach a deployment through this gate alone.
    """
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


def _refs_key_parameter(node: Any) -> bool:
    return _intrinsic(node, "Ref") == KEY_PARAMETER


def _hardcodes_retention(value: Any) -> bool:
    """True unless every present branch of `RetentionInDays` refs a known parameter.

    Requiring a `Ref` to a name in `RETENTION_PARAMETERS` — rather than accepting
    "anything that is not a bare literal" — closes three bypasses that the looser
    form waved through, all three now pinned in
    `test_rule_2_catches_hardcoded_retention`:

    * `!Sub "30"` is a hardcoded thirty. The loader represents it as a dict, so a
      check for "is this branch a dict?" accepts it. It is precisely the literal
      this rule's own failure message tells the author to replace.
    * `!Ref DataRetentionInDays` and `!FindInMap [Map, days, thirty]` are a
      parameter and a mapping, just not the right ones. They defeat the rule's
      purpose as squarely as a literal does: an operator changing
      `LogRetentionDays` would not move a group pinned to either.
    """
    for branch in _branches(value):
        if branch is None or _is_no_value(branch):
            # Absent retention is the sibling gate's rule 2, not this one. Not
            # re-asserted here, so a change there cannot be masked by a pass here.
            continue
        if _intrinsic(branch, "Ref") not in RETENTION_PARAMETERS:
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
# Category membership, derived from the templates
# ---------------------------------------------------------------------------
def _has_optional_key_parameter(doc: dict) -> bool:
    """True if the template takes the key ARN as an OPTIONAL parameter.

    `Default: ''` is what makes it optional, and it is the whole basis of the
    third category: it is the template telling us the key may legitimately be
    absent, which is what makes the conditional `KmsKeyId` write correct there and
    an unconditional one wrong. A mandatory `Type: String` with no `Default`
    (`patterns/unified`, `nested/api-resolvers`, `nested/multi-doc-discovery`) is
    the opposite statement and belongs in `CMK_TEMPLATES`.
    """
    param = (doc.get("Parameters") or {}).get(KEY_PARAMETER)
    return isinstance(param, dict) and param.get("Default") == ""


def _discover_log_group_templates(root: Path | None = None) -> list[str]:
    """Every template under `root` that declares an `AWS::Logs::LogGroup`.

    Discovery is by CONTENT, not by a hardcoded glob list, so a new template cannot
    be added without a category — the same choice `make cfn-lint` and the sibling
    gate's `test_every_template_with_lambdas_is_listed` make, and for the same
    reason: every name list in this area has been found stale at least once.

    Globs both `*.yaml` and `*.yml`; `notebooks/examples/demo-lambda/template.yml`
    is a real log-group-declaring template that a `template.yaml`-only sweep misses.
    The substring pre-filter only ever over-includes — a comment mentioning the type
    is then rejected by parsing — so it cannot hide a template.
    """
    root = (root or REPO_ROOT).resolve()
    # `scratch/` and `.claude/` are the repo's gitignored local-work directories and
    # both hold whole git worktrees (`.claude/worktrees/agent-*/`), i.e. full copies
    # of every template. `tracked_paths` excludes them by asking git, which is what
    # makes this sweep work from *inside* one of those worktrees: matching these
    # names against a file's ABSOLUTE path discards the whole checkout, because the
    # worktree itself lives under `.claude/`. That left rules 4 and 5 with an empty
    # parametrisation and tripped this module's own self-guard. See repo_files.py.
    # The set is kept as a second filter on the repo-RELATIVE path so the fallback
    # walk (synthetic trees in `tmp_path`) and the git listing agree.
    skip_dirs = {
        ".aws-sam",
        "node_modules",
        ".venv",
        "build",
        "dist",
        ".git",
        "scratch",
        ".claude",
    }
    found = []
    for path in tracked_paths(root, "*.yaml", "*.yml"):
        # Meta-tests elsewhere write synthetic probe templates; skip them so a
        # parallel run (`pytest -n auto`) cannot see another worker's probe.
        if path.name.startswith("_") and path.name.endswith(
            ("_probe.yaml", "_probe.yml")
        ):
            continue
        if any(part in skip_dirs for part in path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if LOG_GROUP_TYPE not in text:
            continue
        try:
            doc = _load_text(text)
        except Exception:
            # Not a parseable CloudFormation document. Broad on purpose: the
            # sweep reads every YAML file in the repo, including CI configs and
            # config_library presets, and one of them failing to parse must not
            # take the gate down. A file that cannot be parsed also cannot
            # declare a log group, so skipping it loses no coverage.
            continue
        if isinstance(doc, dict) and _log_groups(doc):
            found.append(str(path.relative_to(root)))
    return sorted(found)


def _category(rel_path: str, root: Path | None = None) -> str | None:
    """Which rule set covers `rel_path`, or None if nothing does."""
    if rel_path in CMK_TEMPLATES:
        return "enforced"
    if rel_path in OUTSIDE_CMK_TEMPLATES:
        return "pinned"
    if _has_optional_key_parameter(_load_at(root or REPO_ROOT, rel_path)):
        return "optional-key"
    return None


def _uncategorised_templates(root: Path | None = None) -> list[str]:
    root = root or REPO_ROOT
    return sorted(
        rel
        for rel in _discover_log_group_templates(root)
        if _category(rel, root) is None
    )


# Derived, deliberately not a name list: any template declaring an optional key
# parameter is covered by rules 4 and 5 the moment it is added. A list would have
# to be edited, and the reason this module needed rewriting is that a list was not.
OPTIONAL_KEY_TEMPLATES = [
    rel for rel in _discover_log_group_templates() if _category(rel) == "optional-key"
]


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
        f"policy change is needed. Do NOT reach for "
        f"`!If [Has..., !Ref ..., !Ref 'AWS::NoValue']` here: these templates take "
        f"the key ARN as a mandatory parameter, so a NoValue branch is unreachable "
        f"code that reads as an accepted gap. If the group belongs to a Lambda that "
        f"runs only during a stack operation, add it to CUSTOM_RESOURCE_ONLY_EXEMPT "
        f"with a cfn_nag W84 suppression and a written reason instead."
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", sorted(CUSTOM_RESOURCE_ONLY_EXEMPT))
def test_exemptions_are_evidenced_and_custom_resource_only(rel_path: str) -> None:
    """The exemption list is checked, not trusted.

    Three things have to hold, and none of them was checked before: the entry is
    not stale; the template itself carries the cfn_nag `W84` suppression with a
    written reason, so the evidence the exemption rests on cannot be deleted while
    the exemption survives; and the log group's owning Lambda really is reachable
    only during a stack operation.

    That last property is re-derived through the sibling gate's
    `custom_resource_only_violation`, which proves the function is a `ServiceToken`
    target (or a `GetAtt`-exported install hook) with no declared event source.
    Its own docstring is candid that this is structural and not airtight, which is
    the honest ceiling for a template-only check; it is still far more than a
    plausible-sounding comment, which is all that stood here before.
    """
    doc = _load(rel_path)
    groups = _log_groups(doc)
    violation = _sibling_module().custom_resource_only_violation

    for lid in sorted(CUSTOM_RESOURCE_ONLY_EXEMPT[rel_path]):
        assert lid in groups, (
            f"{rel_path}: exempt resource {lid!r} is not an AWS::Logs::LogGroup in "
            f"this template (renamed or removed?). Drop the exemption."
        )

        assert _w84_suppression_reason(groups[lid]), (
            f"{rel_path}: {lid} is exempt from rule 1 but carries no cfn_nag W84 "
            f"suppression with a `reason:`. The suppression is the evidence the "
            f"exemption rests on and the only trace of it a reader of the template "
            f"sees; keep them together or delete both."
        )

        owner = _owning_function(doc, lid)
        assert owner is not None, (
            f"{rel_path}: no Lambda declares "
            f"`LoggingConfig.LogGroup: !Ref {lid}`, so this gate cannot tell which "
            f"function writes to it and cannot check that the function runs only "
            f"during a stack operation. Wire the group to its function, or drop the "
            f"exemption and encrypt the group."
        )

        assert violation(rel_path, owner) is None, (
            f"{rel_path}: {lid} is exempt as custom-resource-only, but its owning "
            f"function {owner} is not: {violation(rel_path, owner)}. A function "
            f"reachable outside a stack operation can log request data, so its log "
            f"group needs the customer-managed key."
        )


def _w84_suppression_reason(body: dict) -> str | None:
    """The `reason:` on this resource's cfn_nag W84 suppression, if it has one.

    W84 is "CloudWatch log group should be encrypted"; `CKV_AWS_158` is checkov's
    equivalent and is written as a `# checkov:skip=` comment, which YAML parsing
    cannot see at all — so W84 is the machine-checkable half of the pair.
    """
    metadata = body.get("Metadata")
    if not isinstance(metadata, dict):
        return None
    cfn_nag = metadata.get("cfn_nag")
    if not isinstance(cfn_nag, dict):
        return None
    for entry in cfn_nag.get("rules_to_suppress") or []:
        if isinstance(entry, dict) and entry.get("id") == "W84":
            reason = entry.get("reason")
            if isinstance(reason, str) and reason.strip():
                return reason
    return None


def _owning_function(doc: dict, log_group_lid: str) -> str | None:
    """The Lambda whose `LoggingConfig.LogGroup` resolves to `log_group_lid`.

    Derived from the wiring rather than from the name: `FooFunctionLogGroup` is a
    convention, and stripping the suffix would happily "find" a function that does
    not write to the group at all.
    """
    for lid, body in (doc.get("Resources") or {}).items():
        if not isinstance(body, dict):
            continue
        if body.get("Type") not in {
            "AWS::Serverless::Function",
            "AWS::Lambda::Function",
        }:
            continue
        target = ((body.get("Properties") or {}).get("LoggingConfig") or {}).get(
            "LogGroup"
        )
        if any(
            _intrinsic(branch, "Ref") == log_group_lid for branch in _branches(target)
        ):
            return lid
    return None


# ---------------------------------------------------------------------------
# Rule 2 — retention comes from a parameter
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("rel_path", CMK_TEMPLATES)
def test_every_log_group_takes_retention_from_a_parameter(rel_path: str) -> None:
    """One group hardcoding retention makes `LogRetentionDays` a partial control.

    Deliberately has no exemption list. Every log group in these templates uses
    `!Ref LogRetentionDays` and nothing else — measured, not assumed — so an empty
    exemption list is a stronger statement than an unused one.

    Scoped to `CMK_TEMPLATES` on purpose. Widening it is not a free strengthening:
    four feature stacks hardcode `30`, the seller-entitlement service uses a
    differently named `LogRetentionInDays`, the OIDC fixture uses `1` and the
    notebook example uses `7`. Each of those is a decision about that stack, not a
    bug this rule should fail on.
    """
    offenders = _hardcoded_retention(_load(rel_path))

    assert not offenders, (
        f"{rel_path}: log group(s) not taking RetentionInDays from a parameter in "
        f"{sorted(RETENTION_PARAMETERS)}: {offenders}. Use "
        f"`RetentionInDays: !Ref LogRetentionDays` so an operator changing the "
        f"stack parameter moves every log group together. Wrapping a literal in an "
        f'intrinsic (`!Sub "30"`) or pointing at a different parameter does not '
        f"satisfy this — both leave the group behind when the parameter changes."
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


# ---------------------------------------------------------------------------
# Rules 4 and 5 — templates whose key parameter is optional
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("rel_path", OPTIONAL_KEY_TEMPLATES)
def test_optional_key_log_groups_reference_the_key_parameter(rel_path: str) -> None:
    """In these templates the conditional form is correct — but only that form.

    A template declaring `CustomerManagedEncryptionKeyArn` with `Default: ''` may
    write `!If [HasKey, !Ref CustomerManagedEncryptionKeyArn, !Ref 'AWS::NoValue']`,
    because the key really can be absent. What it may NOT do is omit `KmsKeyId`
    altogether, or point it somewhere other than that parameter: the key is right
    there, wired in and ready.

    This is the rule that was entirely missing. All six templates in this category
    were in neither of the first revision's two sets, so a log group added here with
    no `KmsKeyId` at all produced a clean run — verified by probe.
    """
    doc = _load(rel_path)
    offenders = {}
    for lid, body in sorted(_log_groups(doc).items()):
        value = (body.get("Properties") or {}).get("KmsKeyId")
        branches = _branches(value)
        keyed = [b for b in branches if _refs_key_parameter(b)]
        stray = [
            b
            for b in branches
            if not _refs_key_parameter(b) and b is not None and not _is_no_value(b)
        ]
        if not keyed:
            offenders[lid] = f"never references {KEY_PARAMETER} (value: {value!r})"
        elif stray:
            offenders[lid] = f"branch(es) not from {KEY_PARAMETER}: {stray!r}"

    assert not offenders, (
        f"{rel_path} takes {KEY_PARAMETER} as an optional parameter, so every log "
        f"group in it must use that parameter: {offenders}. Write "
        f"`KmsKeyId: !If [<HasKeyCondition>, !Ref {KEY_PARAMETER}, "
        f"!Ref 'AWS::NoValue']`, matching the other log groups in this template."
    )


@pytest.mark.unit
def test_optional_key_nested_stacks_are_always_passed_the_key() -> None:
    """The third category's premise, asserted instead of assumed.

    Accepting a `NoValue` branch in these templates is only defensible because the
    condition is true in every real deployment — which is a fact about the PARENT,
    not about the template the rule is relaxed for. Unchecked, the category would be
    a promise nothing keeps: someone could stop passing the key and every log group
    in `main-stack-extensions` would silently fall back to the AWS-owned key with
    this gate still green.

    Note what is and is not required. The nested STACK may be conditional
    (`FeaturePlatformStack` carries `Condition: IsFeaturePlatformEnabled`); that only
    decides whether it is deployed at all. What must be unconditional is the
    parameter VALUE and the key resource behind it, so that whenever the stack does
    deploy, it deploys with the key.

    Templates in this category that `template.yaml` never instantiates — the
    `samples/lambda-hook-inference/GENAIIDP-*` hooks — are genuinely deployed
    standalone by customers, so there is no parent to check and an optional key is
    correct outright.
    """
    resources = _load("template.yaml").get("Resources") or {}
    nested = {
        rel: _nested_stack_instantiations(resources, rel)
        for rel in OPTIONAL_KEY_TEMPLATES
    }
    nested = {rel: found for rel, found in nested.items() if found}

    assert nested, (
        f"no template in OPTIONAL_KEY_TEMPLATES ({OPTIONAL_KEY_TEMPLATES}) is "
        f"instantiated as a nested stack by template.yaml any more, so this test "
        f"now proves nothing. feature-platform/main-stack-extensions/template.yaml "
        f"was the known instance — if it was renamed or moved, update the matching; "
        f"if the relaxed category no longer has a member inside the main "
        f"deployment, delete this test rather than leaving it vacuous."
    )

    for rel_path, instantiations in sorted(nested.items()):
        for lid, value in instantiations:
            assert value is not None, (
                f"template.yaml: nested stack {lid} deploys {rel_path}, which "
                f"treats {KEY_PARAMETER} as optional, but the parent passes no "
                f"value for it — so every log group in that stack falls back to "
                f"the AWS-owned key. Pass "
                f"`{KEY_PARAMETER}: !GetAtt CustomerManagedEncryptionKey.Arn`."
            )

            target = _getatt_target(value)
            assert target is not None, (
                f"template.yaml: nested stack {lid} passes {KEY_PARAMETER} as "
                f"{value!r}, which is not a direct `!GetAtt` on a key resource. "
                f"This gate can only prove the key is always present when the "
                f"value is one."
            )
            assert target in resources, (
                f"template.yaml: nested stack {lid} passes {KEY_PARAMETER} from "
                f"{target!r}, which is not a resource in template.yaml."
            )
            assert "Condition" not in resources[target], (
                f"template.yaml: {target} now carries "
                f"`Condition: {resources[target].get('Condition')}`, so the key "
                f"passed to {lid} can be absent at deploy time. Every log group in "
                f"{rel_path} would then fall back to the AWS-owned key while rule 4 "
                f"still passes. Either keep the key unconditional, or move "
                f"{rel_path} into OUTSIDE_CMK_TEMPLATES with that reason recorded."
            )


def _nested_stack_instantiations(
    resources: dict, rel_path: str
) -> list[tuple[str, Any]]:
    """(logical id, KEY_PARAMETER value) per nested stack deploying `rel_path`.

    `TemplateURL` points at `<dir>/.aws-sam/packaged.yaml`, a build artifact, not at
    the source template — the same indirection that makes cfn-lint's E3043 useless
    here — so the match is on the template's DIRECTORY.
    """
    directory = str(Path(rel_path).parent)
    found = []
    for lid, body in resources.items():
        if not isinstance(body, dict):
            continue
        if body.get("Type") != "AWS::CloudFormation::Stack":
            continue
        properties = body.get("Properties") or {}
        url = properties.get("TemplateURL")
        if not isinstance(url, str):
            continue
        if not url.removeprefix("./").startswith(f"{directory}/"):
            continue
        found.append((lid, (properties.get("Parameters") or {}).get(KEY_PARAMETER)))
    return found


def _getatt_target(node: Any) -> str | None:
    attr = _intrinsic(node, "GetAtt")
    if isinstance(attr, str):
        return attr.split(".")[0]
    if isinstance(attr, list) and attr:
        return str(attr[0])
    return None


# ---------------------------------------------------------------------------
# Completeness of the categories
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_every_log_group_template_is_categorised() -> None:
    """Every log-group-declaring template is covered by some rule.

    This is the test whose absence let the first revision ship with eleven log
    groups across six templates that no rule mentioned. The two sets were checked
    for OVERLAP but never for COMPLETENESS, which is a weaker property and the less
    useful of the two: an overlapping template is enforced twice, an uncovered one
    is enforced never.

    The sibling gate learned this the same way — `samples/lambda-hook-inference` and
    then `notebooks/examples/demo-lambda/template.yml` were each missed for a
    release — and closed it with the same content-based sweep.
    """
    uncategorised = _uncategorised_templates()
    assert not uncategorised, (
        f"template(s) declare {LOG_GROUP_TYPE} but no rule in this module covers "
        f"them: {uncategorised}. Add each to CMK_TEMPLATES (if it owns the key or "
        f"takes its ARN as a mandatory parameter), or to OUTSIDE_CMK_TEMPLATES with "
        f"a written reason and the exact set of groups lacking KmsKeyId (an empty "
        f"set is fine and is the stronger statement). A template declaring "
        f"{KEY_PARAMETER} with `Default: ''` joins the derived optional-key "
        f"category automatically and needs no edit here."
    )


@pytest.mark.unit
def test_the_completeness_sweep_can_actually_fail(tmp_path: Path) -> None:
    """The sweep above must be able to fail, or it guarantees nothing.

    Without this, emptying `_discover_log_group_templates` leaves the suite green
    and the completeness guarantee silently gone — which is the exact defect this
    module exists to catch, one level up.

    The probe goes in `tmp_path`, never in the repo: the sibling gate wrote its
    equivalent into `scripts/tests/` and a repo-wide sweep in another test then
    found it under `pytest -n 8`, a deterministic parallel-run failure, and a hard
    kill between write and unlink leaked the file and broke every later run.
    """
    probe = tmp_path / "some-service" / "template.yaml"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        "Resources:\n"
        "  ProbeLogGroup:\n"
        "    Type: AWS::Logs::LogGroup\n"
        "    Properties:\n"
        "      RetentionInDays: 30\n"
    )
    assert _discover_log_group_templates(tmp_path) == ["some-service/template.yaml"]
    assert _uncategorised_templates(tmp_path) == ["some-service/template.yaml"]


@pytest.mark.unit
def test_a_template_with_an_optional_key_parameter_is_categorised(
    tmp_path: Path,
) -> None:
    """The derived category is what keeps the completeness sweep from nagging.

    The complement of the test above: a template that declares the key parameter as
    optional needs no edit to any list in this module, which is the whole point of
    deriving membership instead of enumerating it.
    """
    probe = tmp_path / "some-feature" / "template.yaml"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        "Parameters:\n"
        f"  {KEY_PARAMETER}:\n"
        "    Type: String\n"
        "    Default: ''\n"
        "Resources:\n"
        "  ProbeLogGroup:\n"
        "    Type: AWS::Logs::LogGroup\n"
        "    Properties:\n"
        f"      KmsKeyId: !If [HasKey, !Ref {KEY_PARAMETER}, !Ref 'AWS::NoValue']\n"
        "      RetentionInDays: 30\n"
    )
    rel = "some-feature/template.yaml"
    assert _discover_log_group_templates(tmp_path) == [rel]
    assert _category(rel, tmp_path) == "optional-key"
    assert _uncategorised_templates(tmp_path) == []


@pytest.mark.unit
def test_the_two_hand_kept_template_lists_do_not_overlap() -> None:
    """A template both enforced and excused would be excused, silently.

    Only these two lists are hand-kept, so only this pair can actually diverge.
    `OPTIONAL_KEY_TEMPLATES` is disjoint from both by construction — `_category`
    tests the two lists first and only falls through to the optional-key predicate
    — so asserting those two pairs as well would add two assertions that cannot
    fail. That is the shape of defect this module exists to catch, so it is not
    worth committing here for the sake of a symmetrical-looking test.
    """
    overlap = set(CMK_TEMPLATES) & set(OUTSIDE_CMK_TEMPLATES)
    assert not overlap, (
        f"template(s) in both CMK_TEMPLATES and OUTSIDE_CMK_TEMPLATES: "
        f"{sorted(overlap)}. Rule 3 pins a known gap, so a template in both would "
        f"be reported as an accepted gap by rule 3 and required to be clean by "
        f"rule 1 at the same time. Pick one."
    )


# ---------------------------------------------------------------------------
# Key-policy assumption
# ---------------------------------------------------------------------------
_LOGS_GRANT_SID = "Allow CloudWatch Logs to use the key"

# The encryption-context key CloudWatch Logs supplies when using a CMK. Its value is
# the log group's ARN.
_LOGS_ARN_CONTEXT = "kms:EncryptionContext:aws:logs:arn"


@pytest.mark.unit
def test_cloudwatch_logs_key_grant_covers_every_log_group_name_shape() -> None:
    """The Logs grant must stay broad enough for every log group this gate requires.

    It does NOT have to stay unscoped, and this test deliberately does not demand
    that. An earlier revision failed on the mere presence of a `Condition`, which
    froze the current posture as an invariant and would have blocked a real
    improvement.

    The distinction the earlier revision missed: the scar this repo carries — the
    comment beside the CloudWatch **Alarms** statement in `template.yaml`, where a
    condition once broke every alarm notification and the only symptom was silence —
    is about a CloudFormation `Condition` gating a whole statement. An IAM
    `Condition` narrowing the encryption context is a different mechanism with a
    different failure mode, and an `ArnLike` on
    `arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:*` would
    confine this grant to one account and region while breaking nothing.

    What a scope may NOT do is name groups. The 114 log groups in the enforced
    templates take five different name shapes — measured, not assumed: 84 declare no
    `LogGroupName` and so take CloudFormation's generated
    `<StackName>-<LogicalId>-<hash>`, 20 use `/<StackName>/...`, 7 use
    `/aws/lambda/...`, 2 use `/aws/vendedlogs/states/...` and one
    `API-Gateway-Execution-Logs_...`. No prefix shorter than the whole ARN covers
    all five, so the resource portion after `log-group:` has to be exactly `*`; a
    narrower pattern produces log groups CloudWatch cannot write to, which is a
    deploy-time or runtime failure rather than a lint error. (84 also cross-checks
    against the "~84 groups that declare no `LogGroupName`" figure in CLAUDE.md's
    log-group naming table, arrived at independently.)
    """
    doc = _load("template.yaml")
    key = (doc.get("Resources") or {}).get("CustomerManagedEncryptionKey")
    assert key is not None, "CustomerManagedEncryptionKey is gone; revisit this gate"

    policy = (key.get("Properties") or {}).get("KeyPolicy") or {}
    matching = [
        stmt
        for stmt in (policy.get("Statement") or [])
        if isinstance(stmt, dict) and _is_cloudwatch_logs_grant(stmt)
    ]
    assert matching, (
        f"no key-policy statement grants CloudWatch Logs use of the key. Expected "
        f"one with `Sid: {_LOGS_GRANT_SID}` or "
        f"`Principal.Service: !Sub logs.${{AWS::URLSuffix}}`."
    )

    problems = {
        stmt.get("Sid") or "<no Sid>": problem
        for stmt in matching
        if (problem := _logs_grant_scope_problem(stmt)) is not None
    }
    assert not problems, f"the CloudWatch Logs key grant is too narrow: {problems}"


def _arn_pattern_text(value: Any) -> str:
    """The literal text of an ARN pattern, resolving `Fn::Sub`.

    Every ARN in these templates is written through `!Sub` so the partition,
    region and account come from pseudo-parameters, so a bare `str(value)` sees
    the *dict* and rejects the only spelling a correct scope can have. That was a
    real defect in this test until a probe applied the scope this docstring calls
    safe and watched it fail. The `${...}` placeholders are deliberately left in:
    the check reads only the tail after `log-group:`, where none of them appear.
    """
    sub = _intrinsic(value, "Sub")
    if sub is not None:
        # Long form is [template, {vars}]; only the template can carry the tail.
        return str(sub[0] if isinstance(sub, list) and sub else sub)
    return str(value)


def _logs_grant_scope_problem(stmt: dict) -> str | None:
    """Why this grant is too narrow for the log groups rule 1 requires, else None.

    Separate from the test so the accept path can be exercised on synthetic
    statements. A scope this gate accepts and a scope it rejects are equally
    important, and only the reject path had coverage before.
    """
    condition = stmt.get("Condition")
    if condition is None:
        return None
    if not isinstance(condition, dict):
        return f"non-mapping Condition: {condition!r}"

    patterns = []
    for operator in ("ArnLike", "StringLike"):
        values = (condition.get(operator) or {}).get(_LOGS_ARN_CONTEXT)
        if values is None:
            continue
        patterns.extend(values if isinstance(values, list) else [values])

    if not patterns:
        return (
            f"conditional on {sorted(condition)}, which this gate cannot evaluate. "
            f"The only scope known to be safe here is an ArnLike or StringLike on "
            f"{_LOGS_ARN_CONTEXT}. Any other condition risks producing log groups "
            f"CloudWatch cannot write to — check it covers all five log-group name "
            f"shapes (see the docstring of the test that calls this), then teach "
            f"this gate about it."
        )

    for pattern in patterns:
        text = _arn_pattern_text(pattern)
        if text != "*" and not text.endswith(":log-group:*"):
            return (
                f"scoped to {text!r}, which does not end in `:log-group:*`. 84 of "
                f"the log groups in the enforced templates take CloudFormation's "
                f"generated `<StackName>-<LogicalId>-<hash>` name, which no "
                f"narrower pattern can match without enumerating hashes that do "
                f"not exist until deploy time. Scoping the account and region is "
                f"fine; scoping the group name is not."
            )
    return None


_SCOPE_PREFIX = "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:"


def _scope_arn(tail: str) -> str:
    """An ARN pattern with `tail` after `log-group:`.

    Concatenated rather than `str.format`ed: the pseudo-parameter braces in the
    prefix are `${...}`, which `format` reads as replacement fields.
    """
    return _SCOPE_PREFIX + tail


@pytest.mark.unit
@pytest.mark.parametrize(
    "case,condition,accepted",
    [
        ("no Condition at all", None, True),
        (
            "ArnLike scoped to account and region, group name left open",
            {"ArnLike": {_LOGS_ARN_CONTEXT: {"!Sub": _scope_arn("*")}}},
            True,
        ),
        (
            "StringLike, same scope",
            {"StringLike": {_LOGS_ARN_CONTEXT: {"!Sub": _scope_arn("*")}}},
            True,
        ),
        (
            "list of patterns, all open at the group name",
            {
                "ArnLike": {
                    _LOGS_ARN_CONTEXT: [
                        {"!Sub": _scope_arn("*")},
                        "*",
                    ]
                }
            },
            True,
        ),
        (
            "scoped to /aws/lambda/ only, missing the 84 generated names",
            {"ArnLike": {_LOGS_ARN_CONTEXT: {"!Sub": _scope_arn("/aws/lambda/*")}}},
            False,
        ),
        (
            "one pattern of two too narrow",
            {
                "ArnLike": {
                    _LOGS_ARN_CONTEXT: [
                        {"!Sub": _scope_arn("*")},
                        {"!Sub": _scope_arn("/${AWS::StackName}/*")},
                    ]
                }
            },
            False,
        ),
        (
            "condition on something else entirely",
            {"StringEquals": {"kms:ViaService": "logs.us-east-1.amazonaws.com"}},
            False,
        ),
    ],
)
def test_the_key_grant_scope_check_accepts_and_rejects_the_right_scopes(
    case: str, condition: dict | None, accepted: bool
) -> None:
    """Both directions, because a false failure here blocks a real improvement.

    The reject cases are the point of the test that runs against `template.yaml`;
    the accept cases exist because the first version of it compared `str(value)`
    against the tail and so rejected `!Sub`, which is the only way this repo can
    write an ARN.
    """
    stmt: dict[str, Any] = {"Sid": _LOGS_GRANT_SID}
    if condition is not None:
        stmt["Condition"] = condition
    problem = _logs_grant_scope_problem(stmt)
    if accepted:
        assert problem is None, f"{case}: rejected a scope that is safe: {problem}"
    else:
        assert problem is not None, f"{case}: accepted a scope that is too narrow"


def _is_cloudwatch_logs_grant(stmt: dict) -> bool:
    """Match the CloudWatch Logs statement by Sid, or by an exact principal.

    Not by `"logs." in str(principal)`, which an earlier revision used: a future
    scoped grant for a logs-adjacent principal such as
    `delivery.logs.${AWS::URLSuffix}` would match that and make the test fail with
    the CloudWatch Logs grant perfectly intact.
    """
    if stmt.get("Sid") == _LOGS_GRANT_SID:
        return True
    service = (stmt.get("Principal") or {}).get("Service")
    for candidate in service if isinstance(service, list) else [service]:
        if _intrinsic(candidate, "Sub") == "logs.${AWS::URLSuffix}":
            return True
    return False


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
        # The three below all passed the first revision, which accepted any dict.
        ("literal wrapped in Fn::Sub", '!Sub "30"'),
        ("Ref to a different parameter", "!Ref DataRetentionInDays"),
        ("Fn::FindInMap", "!FindInMap [SomeMap, days, thirty]"),
        ("long-form Ref to a different parameter", "\n        Ref: OtherRetention"),
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
        f"rule 2 failed to flag retention not taken from a known parameter: {case}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "case,value",
    [
        ("short-form Ref", "!Ref LogRetentionDays"),
        ("long-form Ref", "\n        Ref: LogRetentionDays"),
        (
            "both Fn::If branches from the parameter",
            "!If [C, !Ref LogRetentionDays, !Ref LogRetentionDays]",
        ),
        (
            "absent on one branch (the sibling gate's rule, not this one)",
            "!If [C, !Ref LogRetentionDays, !Ref 'AWS::NoValue']",
        ),
    ],
)
def test_rule_2_accepts_retention_from_the_parameter(case: str, value: str) -> None:
    """The tightened rule must not flag the form the whole repo actually uses."""
    body = f"""
Resources:
  GoodLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      KmsKeyId: !Ref CustomerManagedEncryptionKeyArn
      RetentionInDays: {value}
"""
    assert _hardcoded_retention(_load_text(body)) == [], (
        f"rule 2 wrongly flagged retention taken from the parameter: {case}"
    )


@pytest.mark.unit
def test_the_cfn_loader_is_this_worktree_not_an_installed_copy() -> None:
    """The loader must come from this tree, and its path must actually exist.

    This replaces a test that asserted `Path(module.__file__).is_relative_to(
    REPO_ROOT)` — true by construction, since `_CFN_YAML` is built from `REPO_ROOT`
    and `importlib` sets `__file__` from the path it was handed. A test that cannot
    fail, inside a module whose thesis is that such tests are the defect, is worse
    than no test.

    Both assertions here can fail. `is_file()` fails if the loader is moved or
    renamed — it is new enough that a refactor plausibly would — and produces a
    clear message instead of the bare `AssertionError: cannot load ...` from
    `_module_from_path`. The `site-packages` check fails if `_CFN_YAML` is ever
    re-pointed at an installed copy, which is the concrete form the "wrong checkout"
    hazard would take.
    """
    assert _CFN_YAML.is_file(), (
        f"the canonical CFN loader is not at {_CFN_YAML}. If it moved, update "
        f"_CFN_YAML; do not fall back to `import idp_sdk`, which reintroduces the "
        f"dependency on an editable install that this module avoids."
    )
    resolved = Path(_cfn_yaml_module().__file__ or "").resolve()
    assert not {"site-packages", "dist-packages"} & set(resolved.parts), (
        f"the CFN loader resolved to an installed copy at {resolved}, not to this "
        f"worktree. The templates would still be read from {REPO_ROOT}, but they "
        f"would be parsed by another checkout's parser."
    )


@pytest.mark.unit
def test_the_enforced_set_is_not_vacuous() -> None:
    """A loader change that returned {} would make every rule above pass silently.

    Two floors rather than one tight total. The first revision required 100 against
    a measured 109, which left nine groups of headroom: moving log groups into a new
    nested stack would have false-failed a gate that only needs to know the set is
    not empty. Per-template `>= 1` catches the realistic breakage (one template
    silently parsing to nothing) more precisely than a global count can, and the
    global floor is set well below the current 114 so it ages.
    """
    counts = {rel: len(_log_groups(_load(rel))) for rel in CMK_TEMPLATES}

    empty = sorted(rel for rel, count in counts.items() if count == 0)
    assert not empty, (
        f"template(s) in CMK_TEMPLATES declare no log groups at all: {empty}. Every "
        f"assertion in this module would pass vacuously for them — the loader or "
        f"the path is probably broken."
    )

    total = sum(counts.values())
    assert total >= 50, (
        f"expected the enforced templates to declare at least 50 log groups, found "
        f"{total} ({counts}) — the loader or the template list is probably broken."
    )
