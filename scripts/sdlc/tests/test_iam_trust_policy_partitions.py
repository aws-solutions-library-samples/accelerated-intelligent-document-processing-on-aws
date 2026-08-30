"""Guards against partition-specific IAM trust policies (issue #632).

Background — the bug this suite exists to prevent
-------------------------------------------------
An IAM role's trust policy (``AssumeRolePolicyDocument``) is set as part of
``iam:CreateRole`` on a fresh deploy, but changing it on an *already existing*
role is a separate ``iam:UpdateAssumeRolePolicy`` call. So a release that adds a
principal to an existing role's trust policy:

* passes every fresh-deploy test, and
* fails mid-upgrade with AccessDenied for anyone whose CloudFormation service
  role or permissions boundary lacks that action — wedging the stack in
  UPDATE_ROLLBACK_FAILED, because the rollback needs the same action and so
  cannot self-recover either.

v0.6.2 hit exactly this: the GovCloud Cognito federated principals were added to
``CognitoAuthorizedRole`` unconditionally, so *commercial* upgrades rewrote the
trust policy for principals that commercial never uses.

Two independent defences, one test module
-----------------------------------------
1. Don't emit another partition's principals on this partition at all
   (``test_commercial_*`` / ``test_govcloud_*``).
2. Make sure the service role we ship *can* mutate a trust policy anyway, so a
   deliberate future change doesn't wedge anyone (``test_service_role_*``).

The renderer below evaluates ``Fn::If`` for a chosen ``AWS::Partition`` and
treats conditions it cannot decide (those keyed off stack *parameters*) as
"either branch is reachable", so the partition assertions hold for every
possible parameter combination rather than one sampled deployment.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

# scripts/sdlc/tests/<this file> -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

# Every template that creates IAM roles in an AWS account — the product stacks,
# the service role operators deploy by hand, and our own SDLC infrastructure.
# Globbed rather than listed so a new nested stack is covered the day it lands;
# test_every_role_declaring_template_is_scanned proves the globs stay complete.
TEMPLATE_GLOBS = (
    "template.yaml",
    "patterns/*/template.yaml",
    "nested/**/template.yaml",
    "options/*/template.yaml",
    "feature-platform/**/template.yaml",
    "iam-roles/**/*.yaml",
    "scripts/sdlc/cfn/*.yml",
)

# `sam build` copies each template into <stack>/.aws-sam/build/. Those copies are
# gitignored build output: scanning them would make this suite depend on whether
# someone had built locally, and could fail CI on a stale artifact. Pruned for
# the same reason scripts/run_all_tests.py prunes them from test discovery.
PRUNE_PATH_MARKERS = ("/.aws-sam/", "/node_modules/", "/.venv/")

# A trust policy containing any of these literals only works on one partition.
# (Fn::Sub'd principals built from ${AWS::URLSuffix} are partition-neutral by
# construction and correctly do not match.)
PARTITION_SPECIFIC_LITERALS = (
    "us-gov",
    ".amazonaws.com.cn",
    "amazonaws-us-gov.com",
)

COMMERCIAL = "aws"
GOVCLOUD = "aws-us-gov"

SERVICE_ROLE_TEMPLATE = (
    REPO_ROOT
    / "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml"
)

# Actions CloudFormation needs to change a trust policy / permissions boundary on
# an existing role. Not needed on create, which is what makes them easy to miss.
UPDATE_ONLY_ROLE_ACTIONS = (
    "iam:UpdateAssumeRolePolicy",
    "iam:PutRolePermissionsBoundary",
    "iam:DeleteRolePermissionsBoundary",
)


# --- CloudFormation YAML loader that PRESERVES intrinsics ----------------------
# The repo's other template inspectors collapse intrinsics to None, which is
# fine for "which properties are present" checks but useless here: the whole
# point is to look at what Fn::If guards.
class CfnLoader(yaml.SafeLoader):
    """SafeLoader (never the unsafe yaml.Loader) plus CFN short-form tags.

    Kept local rather than taken from idp_sdk._core.cfn_yaml: the long-form
    normalization below is specific to this file's Fn::If evaluation, and these
    tests are meant to run from the repo root with nothing installed.
    """


def _intrinsic(loader, tag_suffix, node):
    key = tag_suffix if tag_suffix in ("Ref", "Condition") else f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        if tag_suffix == "GetAtt":
            value = value.split(".")
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {key: value}


CfnLoader.add_multi_constructor("!", _intrinsic)

_NO_VALUE = {"Ref": "AWS::NoValue"}


def load_template(path: Path) -> dict:
    # CfnLoader subclasses yaml.SafeLoader, so no Python-object construction is
    # possible; input is a developer-committed template from this repo. The
    # loader is driven directly rather than via `yaml.load(..., Loader=)` —
    # identical behaviour, minus the call shape scanners flag. See
    # idp_sdk._core.cfn_yaml.
    with path.open() as f:
        loader = CfnLoader(f)
        try:
            return loader.get_single_data() or {}
        finally:
            loader.dispose()


# --- Three-valued condition evaluation ----------------------------------------
# True / False / None(=undecidable, because it depends on a stack parameter).
def _find_in_map(node, mappings: dict, partition: str):
    """Resolve an Fn::FindInMap keyed off AWS::Partition, else None.

    This repo already maps values per partition (see the console-domain Mapping
    in template.yaml), so a partition-specific principal could reach a trust
    policy through a Mapping rather than a literal. Resolving it here means the
    commercial-render check sees the real string instead of an opaque dict — i.e.
    a FindInMap cannot be used to smuggle a GovCloud principal past this suite.
    """
    if not (isinstance(node, dict) and set(node) == {"Fn::FindInMap"}):
        return None
    args = node["Fn::FindInMap"]
    if not (isinstance(args, list) and len(args) == 3):
        return None
    name, top, second = (_literal(arg, mappings, partition) for arg in args)
    if None in (name, top, second):
        return None
    value = mappings.get(name, {}).get(top, {})
    return value.get(second) if isinstance(value, dict) else None


def _literal(node, mappings: dict, partition: str):
    """Resolve a node to a literal string if possible, else None."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict) and set(node) == {"Ref"}:
        return partition if node["Ref"] == "AWS::Partition" else None
    return _find_in_map(node, mappings, partition)


def _eval_condition(expr, conditions: dict, mappings: dict, partition: str):
    if isinstance(expr, str):  # bare condition name (from Fn::If)
        return _eval_condition(conditions.get(expr), conditions, mappings, partition)
    if not isinstance(expr, dict):
        return None

    if "Condition" in expr:
        return _eval_condition(
            conditions.get(expr["Condition"]), conditions, mappings, partition
        )
    if "Fn::Equals" in expr:
        left, right = expr["Fn::Equals"]
        left = _literal(left, mappings, partition)
        right = _literal(right, mappings, partition)
        if left is None or right is None:
            return None  # depends on a parameter
        return left == right
    if "Fn::Not" in expr:
        inner = _eval_condition(expr["Fn::Not"][0], conditions, mappings, partition)
        return None if inner is None else not inner
    if "Fn::And" in expr:
        parts = [
            _eval_condition(p, conditions, mappings, partition) for p in expr["Fn::And"]
        ]
        if False in parts:
            return False
        return None if None in parts else True
    if "Fn::Or" in expr:
        parts = [
            _eval_condition(p, conditions, mappings, partition) for p in expr["Fn::Or"]
        ]
        if True in parts:
            return True
        return None if None in parts else False
    return None


def render_branches(
    node, conditions: dict, partition: str, mappings: dict | None = None
) -> list:
    """Every value ``node`` can take when AWS::Partition == ``partition``.

    Fn::If on a decidable condition collapses to the taken branch; on an
    undecidable (parameter-driven) one it yields both. Returns a list of
    renderings so callers can assert over all of them.
    """
    mappings = mappings or {}

    if isinstance(node, dict) and set(node) == {"Fn::If"}:
        name, then_branch, else_branch = node["Fn::If"]
        verdict = _eval_condition(name, conditions, mappings, partition)
        if verdict is True:
            candidates = [then_branch]
        elif verdict is False:
            candidates = [else_branch]
        else:
            candidates = [then_branch, else_branch]
        return [
            rendering
            for candidate in candidates
            for rendering in render_branches(candidate, conditions, partition, mappings)
        ]

    # Partition-keyed Mapping lookups collapse to the value for this partition,
    # so a mapped principal is checked as the string it will actually render to.
    resolved = _find_in_map(node, mappings, partition)
    if resolved is not None:
        return [resolved]

    if isinstance(node, list):
        renderings = [[]]
        for item in node:
            expanded = []
            for prefix in renderings:
                for value in render_branches(item, conditions, partition, mappings):
                    if value == _NO_VALUE:  # AWS::NoValue drops the element
                        expanded.append(prefix)
                    else:
                        expanded.append(prefix + [value])
            renderings = expanded
        return renderings

    if isinstance(node, dict) and "Fn::If" not in node:
        renderings = [{}]
        for key, raw in node.items():
            expanded = []
            for prefix in renderings:
                for value in render_branches(raw, conditions, partition, mappings):
                    if value == _NO_VALUE:
                        expanded.append(prefix)
                    else:
                        expanded.append({**prefix, key: value})
            renderings = expanded
        return renderings

    return [node]


def _strings(node):
    """Every string anywhere inside a nested structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def iter_roles(template: dict):
    for logical_id, resource in (template.get("Resources") or {}).items():
        if isinstance(resource, dict) and resource.get("Type") == "AWS::IAM::Role":
            properties = resource.get("Properties")
            if isinstance(properties, dict):
                yield logical_id, properties


def _template_paths() -> list[Path]:
    paths = set()
    for pattern in TEMPLATE_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            as_posix = f"/{path.relative_to(REPO_ROOT).as_posix()}"
            if not any(marker in as_posix for marker in PRUNE_PATH_MARKERS):
                paths.add(path)
    return sorted(paths)


TEMPLATE_PATHS = _template_paths()


def _role_trust_renderings(partition: str):
    """(template, logical_id, rendered trust policy) for one partition."""
    for path in TEMPLATE_PATHS:
        template = load_template(path)
        conditions = template.get("Conditions") or {}
        mappings = template.get("Mappings") or {}
        for logical_id, properties in iter_roles(template):
            document = properties.get("AssumeRolePolicyDocument")
            if document is None:
                continue
            for rendering in render_branches(document, conditions, partition, mappings):
                yield path, logical_id, rendering


# --- The guards ---------------------------------------------------------------
def test_template_glob_is_not_vacuous():
    """A silently-empty scan is the failure mode this whole module guards against."""
    assert len(TEMPLATE_PATHS) >= 10, (
        f"Only found {len(TEMPLATE_PATHS)} templates via TEMPLATE_GLOBS — the globs "
        "have gone stale and the partition checks below are passing vacuously."
    )
    assert REPO_ROOT / "template.yaml" in TEMPLATE_PATHS
    roles = list(_role_trust_renderings(COMMERCIAL))
    assert len(roles) >= 20, (
        f"Only found {len(roles)} IAM role trust policies to check."
    )


def test_every_role_declaring_template_is_scanned():
    """Coverage guard: no tracked template may declare a role we never look at.

    Asserting a floor on the count (above) does not prove *which* templates are
    covered — a new nested stack outside the globs would slip through while the
    count still looked healthy. This asks git for the ground truth instead.
    """
    tracked = subprocess.run(
        ["git", "grep", "-l", "AWS::IAM::Role", "--", "*.yaml", "*.yml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    scanned = {str(path.relative_to(REPO_ROOT)) for path in TEMPLATE_PATHS}
    unscanned = {
        path
        for path in tracked
        if path not in scanned
        and not any(marker in f"/{path}" for marker in PRUNE_PATH_MARKERS)
    }
    assert not unscanned, (
        "These tracked templates declare AWS::IAM::Role but no glob in "
        f"TEMPLATE_GLOBS reaches them: {sorted(unscanned)}"
    )


def test_no_partition_specific_principals_on_commercial():
    """No commercial rendering may contain a GovCloud/China-only principal.

    Such a principal is dead weight at runtime (STS only ever exercises the
    principal for the deploy region) but live at deploy time: it makes the
    trust-policy document differ from the partition-neutral one, which turns
    every upgrade into an iam:UpdateAssumeRolePolicy call. Gate it on a
    partition Condition instead — see IsGovCloudPartition in template.yaml.
    """
    offenders = []
    for path, logical_id, document in _role_trust_renderings(COMMERCIAL):
        for text in _strings(document.get("Statement", [])):
            for literal in PARTITION_SPECIFIC_LITERALS:
                if literal in text:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}::{logical_id} -> {text!r} "
                        f"(matched {literal!r})"
                    )
    assert not offenders, (
        "Partition-specific principal(s) present in a COMMERCIAL trust-policy "
        "rendering:\n  " + "\n  ".join(offenders) + "\n\n"
        "Wrap them in !If [IsGovCloudPartition, {...}, !Ref 'AWS::NoValue'] so the "
        "commercial document stays partition-neutral. See issue #632."
    )


def test_commercial_cognito_trust_policy_is_the_pre_govcloud_document():
    """Pin the exact commercial document, byte-for-byte with pre-v0.6.2.

    The whole fix is "commercial renders what it rendered before, so CloudFormation
    sees no diff". A test that only counted statements would not catch a reordering
    or a whitespace-level change to the surviving statement, which would still
    trigger the UpdateAssumeRolePolicy call.
    """
    template = load_template(REPO_ROOT / "template.yaml")
    conditions = template.get("Conditions") or {}
    role = dict(iter_roles(template))["CognitoAuthorizedRole"]

    renderings = render_branches(
        role["AssumeRolePolicyDocument"], conditions, COMMERCIAL
    )
    assert len(renderings) == 1, "Trust policy must not depend on stack parameters."

    # `Version: 2012-10-17` is unquoted in the template, so PyYAML hands back a
    # date object. CloudFormation itself is fine with that; normalise so the
    # comparison below is about the statement, not YAML scalar typing.
    document = dict(renderings[0], Version=str(renderings[0]["Version"]))

    assert document == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": "cognito-identity.amazonaws.com"},
                "Action": ["sts:AssumeRoleWithWebIdentity"],
                "Condition": {
                    "StringEquals": {
                        "cognito-identity.amazonaws.com:aud": {"Ref": "IdentityPool"}
                    },
                    "ForAnyValue:StringLike": {
                        "cognito-identity.amazonaws.com:amr": "authenticated"
                    },
                },
            }
        ],
    }


def test_govcloud_cognito_trust_policy_keeps_every_region_principal():
    """GovCloud must keep all three statements.

    The Cognito Identity federated principal is per *region*, not per partition
    (us-gov-west-1 uses cognito-identity-us-gov, us-gov-east-1 its own), and
    dropping the commercial statement would churn the trust policy of GovCloud
    stacks already deployed on v0.6.2+ for no benefit.
    """
    template = load_template(REPO_ROOT / "template.yaml")
    conditions = template.get("Conditions") or {}
    role = dict(iter_roles(template))["CognitoAuthorizedRole"]

    (rendering,) = render_branches(
        role["AssumeRolePolicyDocument"], conditions, GOVCLOUD
    )
    principals = {s["Principal"]["Federated"] for s in rendering["Statement"]}
    assert principals == {
        "cognito-identity.amazonaws.com",
        "cognito-identity-us-gov.amazonaws.com",
        "cognito-identity.us-gov-east-1.amazonaws.com",
    }


def test_govcloud_condition_keys_match_their_statement_principal():
    """Each statement's OIDC condition-key namespace must match its principal.

    These namespaces are literal map *keys*, so they cannot be built with
    Fn::Sub — they are hand-copied per statement, and a copy-paste slip yields a
    trust policy that validates fine but never authorizes anyone (the original
    GovCloud symptom: InvalidIdentityPoolConfigurationException at login).
    """
    template = load_template(REPO_ROOT / "template.yaml")
    conditions = template.get("Conditions") or {}
    role = dict(iter_roles(template))["CognitoAuthorizedRole"]

    (rendering,) = render_branches(
        role["AssumeRolePolicyDocument"], conditions, GOVCLOUD
    )
    for statement in rendering["Statement"]:
        principal = statement["Principal"]["Federated"]
        keys = [k for block in statement["Condition"].values() for k in block]
        assert keys, f"{principal} statement has no condition keys"
        for key in keys:
            assert key.startswith(f"{principal}:"), (
                f"condition key {key!r} does not belong to principal {principal!r}"
            )


# --- Defence 2: the service role we ship can mutate a trust policy ------------
def _service_role_iam_actions() -> set[str]:
    template = load_template(SERVICE_ROLE_TEMPLATE)
    actions = set()
    for _, properties in iter_roles(template):
        for policy in properties.get("Policies") or []:
            for statement in (policy.get("PolicyDocument") or {}).get(
                "Statement"
            ) or []:
                raw = statement.get("Action") or []
                for action in [raw] if isinstance(raw, str) else raw:
                    if isinstance(action, str) and action.startswith("iam:"):
                        actions.add(action)
    return actions


@pytest.mark.parametrize("action", UPDATE_ONLY_ROLE_ACTIONS)
def test_service_role_allows_update_only_role_actions(action):
    """The shipped CFN service role must be able to change an existing role.

    Without these, ANY future release that edits a role's trust policy — or any
    change to the PermissionsBoundaryArn parameter — fails mid-update and wedges
    the stack in UPDATE_ROLLBACK_FAILED. Fresh deploys pass either way, so this
    gap is invisible until an upgrade. See issue #632.
    """
    actions = _service_role_iam_actions()
    assert action in actions or "iam:*" in actions, (
        f"{SERVICE_ROLE_TEMPLATE.name} does not grant {action}."
    )


def test_service_role_docs_list_the_same_iam_actions():
    """README must not drift from the template operators actually deploy."""
    readme = (SERVICE_ROLE_TEMPLATE.parent / "README.md").read_text()
    documented = set(re.findall(r"iam:[A-Za-z]+", readme))
    undocumented = _service_role_iam_actions() - documented
    assert not undocumented, (
        f"IAM actions granted but not documented: {sorted(undocumented)}"
    )


# ---------------------------------------------------------------------------
# Finding from review: the AWS_PARTITION env-var wiring had nothing pinning it.
#
# query_knowledgebase_resolver builds the Bedrock inference-profile MODEL_ARN
# itself and reads the partition from AWS_PARTITION, defaulting to "aws". Delete
# the env var from the template and the resolver silently reverts to the exact
# bug — every Knowledge Base query failing in GovCloud on an invalid model ARN —
# with no test failing and the Python arn:aws: gate seeing nothing wrong, because
# the Python is partition-correct and the TEMPLATE is what broke.
# ---------------------------------------------------------------------------

_KB_RESOLVER_TEMPLATE = "nested/api-resolvers/template.yaml"
_KB_RESOLVER_SRC = (
    "nested/api-resolvers/src/lambda/query_knowledgebase_resolver/index.py"
)


def _repo_root_path():
    from pathlib import Path

    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("repo root not found")


def test_kb_resolver_receives_the_partition_from_the_template():
    """The resolver's MODEL_ARN partition must be injected, not defaulted."""
    text = (_repo_root_path() / _KB_RESOLVER_TEMPLATE).read_text()
    assert "AWS_PARTITION: !Ref AWS::Partition" in text, (
        "query_knowledgebase_resolver builds its Bedrock MODEL_ARN from "
        "AWS_PARTITION and falls back to 'aws' when unset. Without this env var "
        "every Knowledge Base query fails in GovCloud on an invalid model ARN, "
        "and no other test or gate notices."
    )


def test_kb_resolver_uses_the_partition_when_building_model_arn():
    """And the resolver must actually USE it rather than a literal arn:aws:."""
    text = (_repo_root_path() / _KB_RESOLVER_SRC).read_text()
    assert 'os.environ.get("AWS_PARTITION")' in text
    assert "arn:{AWS_PARTITION}:bedrock:" in text, (
        "MODEL_ARN must interpolate AWS_PARTITION; a literal arn:aws: cannot "
        "resolve outside the commercial partition."
    )
