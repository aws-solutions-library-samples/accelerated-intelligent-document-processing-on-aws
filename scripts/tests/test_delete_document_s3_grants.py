# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``DeleteDocumentResolverFunction`` may call no S3 operation it is not granted.

What went wrong, and why reading the policy did not catch it
------------------------------------------------------------

``delete_single_document`` purges a deleted document's output bytes. The output
bucket is versioning-enabled and document version history pins prior runs' objects
as noncurrent versions, so the purge enumerates them with a ``list_object_versions``
paginator and deletes them **by** ``VersionId``. The resolver's policy list grants S3
entirely through two SAM ``S3CrudPolicy`` entries, and that managed template contains
neither of the versioned actions — measured from the installed ``samtranslator``, it
grants ``s3:ListBucket`` and ``s3:DeleteObject``, the unversioned forms. So the
enumeration was refused on every deployment and the versioned delete would have been
refused per key (#1366).

The policy was reviewed more than once and read as correct, because
``S3CrudPolicy``'s *name* says crud and a reader does not expand it. So this gate
expands it, from the authority: SAM's own ``policy_templates.json``. Both halves of
the comparison are then derived from source — the operations from the Python, the
granted actions from the template plus SAM's expansion of it — and the only authored
part is the operation-to-action table below, which is the part AWS documents and
neither artifact encodes.

⚠️ **This cannot prove the deployed role works, and nothing offline can.** A policy
that expands correctly is still subject to the bucket policy, an SCP, a permissions
boundary and the KMS key policy, none of which is in this repository. What it proves
is the half that was wrong: that every S3 operation this code path issues has a
matching action in the function's own identity policy.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATE = "nested/api-resolvers/template.yaml"
FUNCTION = "DeleteDocumentResolverFunction"

#: The module whose S3 calls the function's policy has to cover. The resolver itself
#: issues none directly — it hands its client to this shared implementation.
DELETE_PATH_MODULE = "lib/idp_common_pkg/idp_common/delete_documents.py"

#: Which IAM action each S3 API operation on the delete path requires.
#:
#: Authored, with the AWS documentation that settles each entry, because neither
#: botocore's service model nor the SAM policy templates encode this mapping — the
#: service model describes the wire shape and says nothing about authorization.
#:
#: The two that matter are the versioned forms, and both are documented as *distinct*
#: from their unversioned siblings:
#:
#: * ``ListObjectVersions`` requires ``s3:ListBucketVersions``, not ``s3:ListBucket``.
#: * ``DeleteObjects``/``DeleteObject`` by ``VersionId`` require
#:   ``s3:DeleteObjectVersion``: "To delete a specific version of an object from a
#:   versioning-enabled bucket, you must have the ``s3:DeleteObjectVersion``
#:   permission." (S3 API reference, DeleteObject/DeleteObjects — Permissions.)
#:
#: ``DeleteObjects`` is listed with **both**, because this code path passes a
#: ``VersionId`` for every key: the version form is what it actually needs and the
#: unversioned one is required of any delete.
OPERATION_REQUIRES: Dict[str, tuple] = {
    "list_object_versions": ("s3:ListBucketVersions",),
    "list_objects_v2": ("s3:ListBucket",),
    "delete_object": ("s3:DeleteObject",),
    "delete_objects": ("s3:DeleteObject", "s3:DeleteObjectVersion"),
    "get_object": ("s3:GetObject",),
    "put_object": ("s3:PutObject",),
    "head_object": ("s3:GetObject",),
}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _function_properties() -> Dict[str, Any]:
    doc = yaml.load((REPO_ROOT / TEMPLATE).read_text(), Loader=_CfnLoader) or {}
    resource = (doc.get("Resources") or {}).get(FUNCTION)
    assert resource, f"{FUNCTION} is not declared in {TEMPLATE}"
    return resource.get("Properties") or {}


def _sam_policy_template_actions(name: str) -> Set[str]:
    """The actions SAM expands a named managed policy template into.

    Read from ``samtranslator``'s own ``policy_templates.json`` — the artifact the
    build consumes — rather than from a copy written out here, which would go stale
    the first time SAM changed the template and would be asserting this file's belief
    about SAM rather than SAM.
    """
    import samtranslator

    data_file = (
        Path(samtranslator.__file__).parent
        / "policy_templates_data"
        / "policy_templates.json"
    )
    templates = json.loads(data_file.read_text())["Templates"]
    assert name in templates, f"{name} is not a SAM policy template in {data_file}"
    actions: Set[str] = set()
    for statement in templates[name]["Definition"]["Statement"]:
        action = statement.get("Action") or []
        actions.update([action] if isinstance(action, str) else action)
    return actions


def _granted_actions(policies: Iterable[Any]) -> Set[str]:
    """Every S3 action the function holds, with SAM's templates expanded."""
    granted: Set[str] = set()
    for entry in policies:
        if not isinstance(entry, dict):
            # A bare string is a managed-policy name or ARN; none is used here, and
            # one appearing would be an expansion this gate cannot perform.
            continue
        for key, value in entry.items():
            if key == "Statement":
                for statement in value:
                    if statement.get("Effect") != "Allow":
                        continue
                    action = statement.get("Action") or []
                    granted.update([action] if isinstance(action, str) else action)
            elif key.startswith("S3"):
                granted.update(_sam_policy_template_actions(key))
    return {a for a in granted if isinstance(a, str) and a.startswith("s3:")}


def _s3_operations_called(module_path: str) -> Set[str]:
    """The S3 API operations ``module_path`` issues, from its source.

    Two shapes, both of which the delete path uses: a method on the client
    (``s3_client.delete_objects(...)``, including the form where the bound method is
    handed to the retry wrapper) and a paginator named by string
    (``get_paginator("list_object_versions")``).
    """
    tree = ast.parse((REPO_ROOT / module_path).read_text())
    found: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get_paginator":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
        # The client is a parameter named `s3_client` throughout this module, so the
        # receiver name is what distinguishes an S3 call from a DynamoDB one.
        candidates = [func] + [
            a for a in list(node.args) + [k.value for k in node.keywords]
        ]
        for candidate in candidates:
            if (
                isinstance(candidate, ast.Attribute)
                and isinstance(candidate.value, ast.Name)
                and candidate.value.id == "s3_client"
            ):
                found.add(candidate.attr)
    return {op for op in found if op in OPERATION_REQUIRES} | {
        op for op in found if op not in OPERATION_REQUIRES and op != "get_paginator"
    }


@pytest.mark.unit
def test_the_delete_path_issues_s3_operations_this_gate_recognises():
    """Non-vacuity, and the premise the comparison below cannot assert about itself.

    An empty derived set would make the coverage assertion pass over nothing. An
    *unrecognised* operation is the more interesting failure: it means the delete path
    gained an S3 call whose required action nobody has written down, so the coverage
    check silently stops covering it.
    """
    called = _s3_operations_called(DELETE_PATH_MODULE)
    assert called, f"no S3 operations were found in {DELETE_PATH_MODULE}"
    unknown = sorted(called - set(OPERATION_REQUIRES))
    assert not unknown, (
        f"{DELETE_PATH_MODULE} calls S3 operations with no entry in "
        f"OPERATION_REQUIRES: {unknown}. Add each one with the AWS documentation for "
        "the action it needs, rather than leaving it uncovered."
    )
    # The two the defect was about, pinned by name: if the purge stopped enumerating
    # or deleting versions, the grant below would become dead rather than wrong.
    assert "list_object_versions" in called
    assert "delete_objects" in called


@pytest.mark.unit
def test_every_s3_operation_on_the_delete_path_is_granted():
    granted = _granted_actions(_function_properties().get("Policies") or [])
    required: Set[str] = set()
    for operation in _s3_operations_called(DELETE_PATH_MODULE):
        required.update(OPERATION_REQUIRES[operation])

    missing = sorted(required - granted)
    assert not missing, (
        f"{FUNCTION} calls S3 operations it is not granted: {missing}. "
        f"Granted (with SAM templates expanded): {sorted(granted)}"
    )


@pytest.mark.unit
def test_sam_s3crudpolicy_does_not_cover_the_versioned_actions():
    """The premise that makes the added statement necessary rather than redundant.

    If a future SAM release folds the versioned actions into ``S3CrudPolicy``, the
    coverage test above keeps passing and the explicit statement becomes dead weight —
    a grant nobody can tell is still load bearing. This states the dependency, so that
    change is a conversation rather than a silent one.
    """
    crud = _sam_policy_template_actions("S3CrudPolicy")
    assert crud, "S3CrudPolicy expanded to nothing"
    assert "s3:ListBucketVersions" not in crud
    assert "s3:DeleteObjectVersion" not in crud


@pytest.mark.unit
@pytest.mark.parametrize(
    "action, expected_suffix",
    [
        ("s3:ListBucketVersions", ""),
        ("s3:DeleteObjectVersion", "/*"),
    ],
)
def test_the_versioned_grants_are_scoped_and_partition_parameterised(
    action, expected_suffix
):
    """Bucket-level actions on the bucket ARN, object-level actions on ``/*``.

    An ``s3:ListBucketVersions`` written against ``…/*`` is granted on nothing, and an
    ``s3:DeleteObjectVersion`` written against the bare bucket ARN likewise — both
    expand, lint and deploy, and both fail at run time with the same ``AccessDenied``
    the missing action produced, which is a confusing way to re-introduce this defect.
    The partition check is the repository's GovCloud rule: a hardcoded ``arn:aws:``
    grants nothing in ``aws-us-gov``.
    """
    statements: List[dict] = []
    for entry in _function_properties().get("Policies") or []:
        if isinstance(entry, dict) and "Statement" in entry:
            statements.extend(entry["Statement"])

    matching = [
        s
        for s in statements
        if action in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    ]
    assert matching, f"{FUNCTION} does not grant {action}"

    for statement in matching:
        resources = statement["Resource"]
        # A single resource arrives either as a plain string or as a one-key
        # ``{"Fn::Sub": ...}`` mapping, and iterating the mapping would yield the key
        # ``"Fn::Sub"`` rather than the ARN — which is a way for this check to assert
        # something about the wrong string entirely.
        if isinstance(resources, (str, dict)):
            resources = [resources]
        for resource in resources:
            rendered = resource["Fn::Sub"] if isinstance(resource, dict) else resource
            assert isinstance(rendered, str), (
                f"{action} resource is not a plain Fn::Sub string: {resource!r}"
            )
            assert rendered.startswith("arn:${AWS::Partition}:s3:::"), (
                f"{action} is granted on {rendered!r}, which hardcodes a partition"
            )
            assert rendered.endswith(expected_suffix or "}"), (
                f"{action} is granted on {rendered!r}; a bucket-level action belongs "
                "on the bucket ARN and an object-level action on the /* form"
            )
