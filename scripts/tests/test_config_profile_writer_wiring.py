# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Lambda that saves a configuration profile can record its revision (#1778).

`ConfigRevisionStore` is enabled only when `CONFIGURATION_BUCKET` is set, and a
disabled store turns the revision cut inside `save_configuration` into a no-op. The
save still rewrites the profile head, so the configuration editor shows the new
configuration, while every new document stays pinned to the previously published
revision and runs the old one. Single-document discovery, multi-document discovery,
the Replace-mode clear, BDA blueprint optimization and sync, the Quick Start agent on
the agent processor, and the data generator's bootstrap all shipped without the
bucket; both chat processors had it but could only read ``config_library/*``, so
their revision write was refused and ``save_configuration`` logged and carried on.
Nothing failed: the save succeeded, the document processed, and the result was
quietly wrong.

The set of writers is **derived** (`config_profile_writers`), so a new Lambda that
saves a profile is held to this without being listed. Every Lambda that is given the
configuration table (under either name in ``CONFIG_TABLE_ENV``) is in the universe,
and one whose source cannot be located fails
rather than being skipped, because an unread Lambda is one the derivation cannot
clear. :data:`KNOWN_WRITERS` is a floor under the derivation, not the list it
checks: it names Lambdas measured to save profiles, so a derivation that silently
stops seeing one of them fails here instead of shrinking the gate.

The grant checks read inline ``Statement`` lists, SAM S3 policy templates and the
inline policies of a role the function names with ``Role: !GetAtt R.Arn``. A grant
made through a managed policy or a separate ``AWS::IAM::Policy`` is not seen and
reads as missing, which is the safe
direction for a gate that only ever asks for a grant to be present.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config_profile_writers as writers
import pytest
import yaml

REPO_ROOT = writers.REPO_ROOT
PREFIX = "config_revisions"

#: Lambdas measured to save a profile when this gate was written. Each is a
#: (template, logical id) pair; the derivation must find at least these.
KNOWN_WRITERS = frozenset(
    {
        ("template.yaml", "DiscoveryProcessorFunction"),
        ("template.yaml", "BlueprintOptimizationFunction"),
        ("template.yaml", "UpdateConfigurationFunction"),
        # Given the table as CONFIGURATION_TABLE, its grants on a shared role.
        (
            "feature-platform/main-stack-extensions/template.yaml",
            "ApplyFeatureConfigPresetFunction",
        ),
        # These three reach the Quick Start agent, whose create_config_version
        # tool saves a profile, only through the agent registry.
        ("template.yaml", "AgentChatProcessorFunction"),
        ("template.yaml", "ChatStreamProcessorFunction"),
        ("template.yaml", "AgentProcessorFunction"),
        ("nested/api-resolvers/template.yaml", "DiscoveryUploadResolverFunction"),
        ("nested/api-resolvers/template.yaml", "ConfigurationResolverFunction"),
        ("nested/api-resolvers/template.yaml", "SyncBdaIdpResolverFunction"),
        ("nested/multi-doc-discovery/template.yaml", "MultiDocDiscoverySaveFunction"),
        (
            "feature-platform/idp-data-generator/template.yaml",
            "BootstrapProcessorFunction",
        ),
    }
)

#: SAM policy templates and the S3 actions each grants on its ``BucketName``.
SAM_S3_TEMPLATES = {
    "S3ReadPolicy": {"s3:GetObject", "s3:ListBucket"},
    "S3WritePolicy": {"s3:PutObject"},
    "S3CrudPolicy": {
        "s3:GetObject",
        "s3:ListBucket",
        "s3:PutObject",
        "s3:DeleteObject",
    },
    "S3FullAccessPolicy": {
        "s3:GetObject",
        "s3:ListBucket",
        "s3:PutObject",
        "s3:DeleteObject",
    },
}
OBJECT_ACTIONS = ("s3:GetObject", "s3:PutObject", "s3:DeleteObject")


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag(loader, suffix, node):
    key = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
        if suffix == "GetAtt":
            value = value.split(".", 1)
        return {key: value}
    if isinstance(node, yaml.SequenceNode):
        return {key: loader.construct_sequence(node, deep=True)}
    return {key: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag)


def _templates() -> List[str]:
    out = subprocess.run(
        ["bash", "scripts/discover_templates.sh", "cfn"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out, "template discovery returned nothing"
    return out


#: The environment variables a deployed Lambda is given the configuration table
#: under. `ConfigurationManager` reads `CONFIGURATION_TABLE_NAME`; the
#: feature-platform handlers are given `CONFIGURATION_TABLE` and bridge it at
#: runtime, and keying on one spelling left them outside the universe.
CONFIG_TABLE_ENV = ("CONFIGURATION_TABLE_NAME", "CONFIGURATION_TABLE")

#: Each template's Resources, for following a function's `Role:` to its policies.
TEMPLATE_RESOURCES: Dict[str, Dict[str, Any]] = {}


def _functions() -> List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]]:
    """(template, logical id, resource, environment) for every config-table Lambda."""
    found = []
    for template in _templates():
        doc = yaml.load((REPO_ROOT / template).read_text(), Loader=_CfnLoader)
        if not isinstance(doc, dict):
            continue
        TEMPLATE_RESOURCES[template] = doc.get("Resources") or {}
        globals_env = (
            ((doc.get("Globals") or {}).get("Function") or {}).get("Environment") or {}
        ).get("Variables") or {}
        for name, resource in (doc.get("Resources") or {}).items():
            if not isinstance(resource, dict) or resource.get("Type") not in (
                "AWS::Serverless::Function",
                "AWS::Lambda::Function",
            ):
                continue
            props = resource.get("Properties") or {}
            env = dict(globals_env)
            env.update(((props.get("Environment") or {}).get("Variables")) or {})
            if any(var in env for var in CONFIG_TABLE_ENV):
                found.append((template, name, resource, env))
    return found


_DOCKER_COPY = re.compile(r"^COPY\s+(src/lambda/[^\s*]+?)/?\*?\.py\s", re.M)
_IMAGE_TAG = re.compile(r":([A-Za-z0-9_-]+?)-\$\{ImageVersion\}$")


def _source_files(template: str, resource: Dict[str, Any]) -> Optional[List[Path]]:
    """The Lambda's own code files, or None if they cannot be located."""
    props = resource.get("Properties") or {}
    base = (REPO_ROOT / template).parent
    code_uri = props.get("CodeUri")
    if isinstance(code_uri, str):
        path = (base / code_uri).resolve()
        return writers.lambda_files(path) if path.is_dir() else None
    if props.get("PackageType") != "Image":
        return None
    image = props.get("ImageUri")
    image = image.get("Fn::Sub") if isinstance(image, dict) else image
    if not isinstance(image, str):
        return None
    source: Optional[Path] = None
    tagged = _IMAGE_TAG.search(image)
    if tagged:
        # patterns/unified: `<name>-function-${ImageVersion}` -> src/<name>_function
        name = tagged.group(1)
        for candidate in (name.replace("-", "_"), name):
            if (base / "src" / candidate).is_dir():
                source = base / "src" / candidate
                break
    elif (base / "Dockerfile").is_file():
        copies = _DOCKER_COPY.findall((base / "Dockerfile").read_text())
        if len(copies) == 1 and (REPO_ROOT / copies[0]).is_dir():
            source = REPO_ROOT / copies[0]
    if source is None:
        return None
    command = ((props.get("ImageConfig") or {}).get("Command")) or []
    if command and isinstance(command[0], str) and "." in command[0]:
        files = writers.entry_closure(source, command[0].rsplit(".", 1)[0])
        return files or None
    return writers.lambda_files(source)


FUNCTIONS = _functions()
FUNCTION_IDS = [f"{t}::{n}" for t, n, _, _ in FUNCTIONS]


def _derived_writers() -> Dict[Tuple[str, str], Tuple[Dict[str, Any], Dict[str, Any]]]:
    out = {}
    for template, name, resource, env in FUNCTIONS:
        files = _source_files(template, resource)
        if files and writers.files_save_a_profile(files):
            out[(template, name)] = (resource, env)
    return out


DERIVED = _derived_writers()
DERIVED_IDS = [f"{t}::{n}" for t, n in DERIVED]


@pytest.mark.parametrize(
    ("template", "name", "resource", "env"), FUNCTIONS, ids=FUNCTION_IDS
)
def test_every_config_table_lambda_has_source_the_derivation_can_read(
    template, name, resource, env
):
    assert _source_files(template, resource), (
        f"{template}::{name} is given the configuration table but its source could "
        "not be located, so whether it saves a profile cannot be decided. Teach "
        "_source_files its packaging shape."
    )


def test_the_derivation_finds_every_measured_writer():
    missing = sorted(KNOWN_WRITERS - set(DERIVED))
    assert not missing, (
        f"the writer derivation no longer sees {missing}; it has lost a resolution "
        "rule (see config_profile_writers), so the gate below checks less than it did"
    )


def _bucket_token(arn: Any) -> Optional[Tuple[str, Any]]:
    """(suffix after the bucket, the bucket's expression) for an S3 ARN resource."""
    if isinstance(arn, dict) and "Fn::Sub" in arn:
        sub = arn["Fn::Sub"]
        text, mapping = (sub, {}) if isinstance(sub, str) else (sub[0], sub[1])
        match = re.fullmatch(r"arn:\$\{AWS::Partition\}:s3:::\$\{([^}]+)\}(.*)", text)
        if not match:
            return None
        var, suffix = match.groups()
        return suffix, mapping.get(var, {"Ref": var})
    return None


def _bucket_expr(env_value: Any) -> Any:
    """The env var's bucket expression in the shape `_bucket_token` returns."""
    if isinstance(env_value, dict) and "Fn::Ref" in env_value:
        return {"Ref": env_value["Fn::Ref"]}
    return env_value


def _role_statements(template: str, resource: Dict[str, Any]) -> List[Any]:
    """Inline statements of the role a function names with `Role: !GetAtt R.Arn`.

    A policy wrapped in `Fn::If` contributes both branches that are policies,
    which is the condition-true one in practice (the other is `AWS::NoValue`).
    """
    role = (resource.get("Properties") or {}).get("Role")
    target = role.get("Fn::GetAtt") if isinstance(role, dict) else None
    if not (isinstance(target, list) and target and target[-1] == "Arn"):
        return []
    role_resource = TEMPLATE_RESOURCES.get(template, {}).get(target[0]) or {}
    if role_resource.get("Type") != "AWS::IAM::Role":
        return []
    statements: List[Any] = []
    for policy in (role_resource.get("Properties") or {}).get("Policies") or []:
        branches = policy.get("Fn::If", [None])[1:] if isinstance(policy, dict) else []
        for candidate in branches if branches else [policy]:
            if isinstance(candidate, dict):
                document = candidate.get("PolicyDocument") or {}
                statements.extend(document.get("Statement") or [])
    return statements


def _grants(
    template: str, resource: Dict[str, Any], bucket: Any
) -> Dict[str, List[str]]:
    """Action -> the resource suffixes on `bucket` that action is granted on."""
    out: Dict[str, List[str]] = {}
    statements: List[Any] = list(_role_statements(template, resource))
    for policy in (resource.get("Properties") or {}).get("Policies") or []:
        if not isinstance(policy, dict):
            continue
        for key, value in policy.items():
            if key in SAM_S3_TEMPLATES and isinstance(value, dict):
                if _bucket_expr(value.get("BucketName")) == bucket:
                    for action in SAM_S3_TEMPLATES[key]:
                        out.setdefault(action, []).extend(["", "/*"])
        statements.extend(policy.get("Statement") or [])
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        actions = statement.get("Action")
        actions = actions if isinstance(actions, list) else [actions]
        resources = statement.get("Resource")
        resources = resources if isinstance(resources, list) else [resources]
        for arn in resources:
            token = _bucket_token(arn)
            if not token or token[1] != bucket:
                continue
            for wanted in (*OBJECT_ACTIONS, "s3:ListBucket"):
                if any(
                    isinstance(a, str)
                    and fnmatch.fnmatchcase(wanted.lower(), a.lower())
                    for a in actions
                ):
                    out.setdefault(wanted, []).append(token[0])
    return out


@pytest.mark.parametrize(("key"), list(DERIVED), ids=DERIVED_IDS)
def test_a_profile_writer_is_told_the_configuration_bucket(key):
    _, env = DERIVED[key]
    assert env.get("CONFIGURATION_BUCKET"), (
        f"{key[0]}::{key[1]} saves configuration profiles but has no "
        "CONFIGURATION_BUCKET, so its saves rewrite the profile head without cutting "
        "a revision and documents keep running the previously published one"
    )


@pytest.mark.parametrize(("key"), list(DERIVED), ids=DERIVED_IDS)
def test_a_profile_writer_can_read_write_and_prune_revision_bodies(key):
    resource, env = DERIVED[key]
    bucket = _bucket_expr(env.get("CONFIGURATION_BUCKET"))
    grants = _grants(key[0], resource, bucket)
    for action in OBJECT_ACTIONS:
        assert set(grants.get(action, [])) & {f"/{PREFIX}/*", "/*"}, (
            f"{key[0]}::{key[1]} has no {action} on {PREFIX}/* of {bucket}"
        )
    assert "" in grants.get("s3:ListBucket", []), (
        f"{key[0]}::{key[1]} has no s3:ListBucket on {bucket}; without it S3 "
        "answers a missing revision key with 403, which the revision store raises on"
    )
