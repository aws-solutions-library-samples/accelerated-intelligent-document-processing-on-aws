# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Both container-image build resources are served by the shared, polling handler.

Issue #1824. The unified pattern's ``DockerBuildRun`` and the
multi-document-discovery stack's ``DockerBuildRun`` run the same job -- start a
CodeBuild build of the stack's images and answer CloudFormation when they are in
ECR -- and they used to be served by different handlers. The multi-document one
waited inside a single 900-second invocation, so a build that queued for longer
than about 14 minutes failed the stack operation, and its rollback rebuilt the
previous version from scratch and failed the same way.

Both are now served by ``src/lambda/start_codebuild``, which polls for
CloudFormation's full hour and lets a rollback reuse images that are still in
ECR. The behaviour is tested beside that handler; this file pins the template
half, which no handler test can see:

* each resource's service-token function is built from that handler;
* each resource names the repository, image version and image set a rollback
  checks, and the version and set compose to the tags the functions pull;
* each handler's role can read its own stack's status and describe its
  repository's images, and no more widely than that.

The templates are read through the repository's CloudFormation-tolerant YAML
loader, loaded by path for the reasons
``patterns/unified/tests/test_expected_images_match_the_buildspec.py`` gives.
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CFN_YAML = _REPO_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "cfn_yaml.py"
_HANDLER_DIR = _REPO_ROOT / "src" / "lambda" / "start_codebuild"

TEMPLATES = {
    "unified": _REPO_ROOT / "patterns" / "unified" / "template.yaml",
    "multi-doc-discovery": _REPO_ROOT
    / "nested"
    / "multi-doc-discovery"
    / "template.yaml",
}


@functools.lru_cache(maxsize=None)
def _cfn_yaml() -> Any:
    spec = importlib.util.spec_from_file_location("_cfn_yaml_for_wiring", _CFN_YAML)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=None)
def _resources(name: str) -> dict:
    text = TEMPLATES[name].read_text(encoding="utf-8")
    return (_cfn_yaml().load_cfn_yaml(text) or {})["Resources"]


def _build_run(name: str) -> dict:
    resource = _resources(name)["DockerBuildRun"]
    assert resource["Type"] in ("Custom::CodeBuildRun", "Custom::DockerBuild")
    return resource


def _service_token_function(name: str) -> tuple[str, dict]:
    token = _build_run(name)["Properties"]["ServiceToken"]
    logical_id = token["!GetAtt"].split(".")[0]
    return logical_id, _resources(name)[logical_id]


def _role_statements(name: str) -> list[dict]:
    _, function = _service_token_function(name)
    role_id = function["Properties"]["Role"]["!GetAtt"].split(".")[0]
    role = _resources(name)[role_id]
    statements = []
    for policy in role["Properties"]["Policies"]:
        statements.extend(policy["PolicyDocument"]["Statement"])
    return statements


def _grants(statements: list[dict], action: str) -> list[Any]:
    found = []
    for statement in statements:
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else actions or []
        if statement.get("Effect") == "Allow" and action in actions:
            found.append(statement["Resource"])
    return found


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_build_resource_is_served_by_the_shared_polling_handler(name):
    _, function = _service_token_function(name)
    code_uri = function["Properties"]["CodeUri"]
    resolved = (TEMPLATES[name].parent / code_uri).resolve()

    assert resolved == _HANDLER_DIR.resolve(), (
        f"{name}: DockerBuildRun's handler is built from {code_uri}, not from "
        f"src/lambda/start_codebuild, so it does not have that handler's hour-long "
        f"wait or its rollback image reuse (#1824)"
    )


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_build_resource_names_what_a_rollback_checks(name):
    properties = _build_run(name)["Properties"]

    assert properties["RepositoryName"] == {"!Ref": "ECRRepository"}
    assert properties["ImageVersion"]
    assert properties["ExpectedImages"]
    assert properties["BuildProjectName"] == {"!Ref": "DockerBuildProject"}


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_handler_can_read_only_its_own_stack_status(name):
    grants = _grants(_role_statements(name), "cloudformation:DescribeStacks")

    assert grants == [{"!Ref": "AWS::StackId"}]


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_handler_can_describe_its_repositorys_images(name):
    grants = _grants(_role_statements(name), "ecr:DescribeImages")
    resources = [
        g for grant in grants for g in (grant if isinstance(grant, list) else [grant])
    ]

    assert {"!GetAtt": "ECRRepository.Arn"} in resources


def test_the_multi_doc_handler_can_schedule_its_own_polls():
    statements = _role_statements("multi-doc-discovery")

    assert _grants(statements, "events:PutRule")
    assert _grants(statements, "lambda:AddPermission")


def test_the_multi_doc_functions_pull_the_tag_the_build_pushes_and_verifies():
    """One content-addressed tag, spelled three ways, which must agree.

    The buildspec pushes ``$IMAGE_TAG``, the handler verifies
    ``<ExpectedImages[0]>-<ImageVersion>``, and the functions pull their
    ``ImageUri``. With a fixed ``latest`` tag a rollback could not tell the
    previous version's image from the new one, and an upgrade left every
    function's ``ImageUri`` unchanged.
    """
    resources = _resources("multi-doc-discovery")
    properties = _build_run("multi-doc-discovery")["Properties"]
    assert properties["ImageVersion"] == {"!Ref": "BuildHash"}
    verified = f"{properties['ExpectedImages'][0]}-${{BuildHash}}"

    env = {
        v["Name"]: v["Value"]
        for v in resources["DockerBuildProject"]["Properties"]["Environment"][
            "EnvironmentVariables"
        ]
    }
    assert env["IMAGE_TAG"] == {"!Sub": verified}
    assert env["IMAGE_VERSION"] == {"!Ref": "BuildHash"}

    image_uris = [
        r["Properties"]["ImageUri"]
        for r in resources.values()
        if r.get("Properties", {}).get("PackageType") == "Image"
    ]
    assert image_uris, "no container-image function found"
    assert set(map(str, image_uris)) == {
        str({"!Sub": "${ECRRepository.RepositoryUri}:" + verified})
    }
