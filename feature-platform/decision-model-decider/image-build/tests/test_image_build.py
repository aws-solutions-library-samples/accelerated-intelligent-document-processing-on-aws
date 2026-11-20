# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The decider image is built from pinned commits, once per set of build inputs.

``crhelper`` is a Lambda dependency (``requirements.txt``), not a test dependency,
so it is stubbed: its decorators only register handlers, and the suite calls them
directly. The template half (what the custom resource is given, what the buildspec
reads and checks, what the function pulls) is read from ``template.yaml`` with the
repository's CloudFormation-tolerant loader.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_FEATURE_DIR = Path(__file__).resolve().parents[2]
_INDEX_PATH = _FEATURE_DIR / "image-build" / "index.py"
_TEMPLATE = _FEATURE_DIR / "template.yaml"
_MODULE_NAME = "decider_image_build_index_under_test"

SOURCE_SHA = "79bf6e7184c166990eec79034667cc0bfd445e12"
MODEL_SHA = "4d27d8d7edcfd6616806c8684e20e2673684e283"


class _FakeCfnResource:
    def __init__(self, *_args, **_kwargs):
        self.Data: dict = {}

    def _passthrough(self, func):
        return func

    create = update = delete = _passthrough
    poll_create = poll_update = _passthrough

    def init_failure(self, exception):  # pragma: no cover - never hit in tests
        raise exception

    def __call__(self, event, context):  # pragma: no cover - handler not exercised
        raise AssertionError("the suite calls the handlers directly")


class _ImageNotFound(Exception):
    pass


@pytest.fixture
def index():
    fake_crhelper = SimpleNamespace(CfnResource=_FakeCfnResource)
    with (
        patch.dict(sys.modules, {"crhelper": fake_crhelper}),
        patch.dict(os.environ, {"AWS_DEFAULT_REGION": "us-east-1"}, clear=False),
        patch("boto3.client", side_effect=lambda name, **_kw: MagicMock(name=name)),
    ):
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    module.ECR.exceptions.ImageNotFoundException = _ImageNotFound
    return module


def _properties(**overrides) -> dict:
    props = {
        "ServiceToken": "arn:aws:lambda:us-east-1:123456789012:function:f",
        "ProjectName": "decider-build",
        "RepositoryName": "decider-repo",
        "SourceRevision": SOURCE_SHA,
        "ModelId": "StrandsAgents/strands-decider-2B-qwen3.5-v1-2610",
        "ModelRevision": MODEL_SHA,
        "Vision": "true",
    }
    props.update(overrides)
    return props


def _event(**overrides) -> dict:
    return {"RequestType": "Create", "ResourceProperties": _properties(**overrides)}


@pytest.fixture(scope="module")
def template() -> dict:
    spec = importlib.util.spec_from_file_location(
        "_cfn_yaml_for_decider",
        _FEATURE_DIR.parents[1]
        / "lib"
        / "idp_sdk"
        / "idp_sdk"
        / "_core"
        / "cfn_yaml.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_cfn_yaml(_TEMPLATE.read_text(encoding="utf-8"))


# --- the pins ---------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("SourceRevision", "main"),
        ("SourceRevision", "v0.3.0"),
        ("SourceRevision", SOURCE_SHA[:12]),
        ("SourceRevision", SOURCE_SHA.upper()),
        ("SourceRevision", SOURCE_SHA + "0"),
        ("SourceRevision", ""),
        ("ModelRevision", "main"),
        ("ModelRevision", "261016"),
        ("ModelRevision", MODEL_SHA[:7]),
        ("ModelRevision", ""),
        ("ModelId", "strands-decider-2B"),
        ("ModelId", "a/b/c"),
        ("ModelId", "../x/y"),
        ("Vision", "yes"),
    ],
)
def test_a_movable_or_malformed_input_is_refused(index, field, value):
    with pytest.raises(ValueError):
        index.build_inputs(_properties(**{field: value}))


def test_the_template_defaults_are_accepted(index, template):
    params = template["Parameters"]
    inputs = index.build_inputs(
        _properties(
            SourceRevision=params["DeciderSourceRevision"]["Default"],
            ModelId=params["ModelId"]["Default"],
            ModelRevision=params["ModelRevision"]["Default"],
            Vision=params["EnableVision"]["Default"],
        )
    )
    assert inputs["SOURCE_REVISION"] == SOURCE_SHA
    assert inputs["MODEL_REVISION"] == MODEL_SHA


@pytest.mark.parametrize(
    "parameter", ["DeciderSourceRevision", "ModelRevision", "ModelId"]
)
def test_the_parameter_pattern_is_the_handler_pattern(index, template, parameter):
    """CloudFormation refuses what the handler would refuse, before anything runs."""
    pattern = template["Parameters"][parameter]["AllowedPattern"]
    handler_pattern = (
        index.HUB_MODEL_ID if parameter == "ModelId" else index.COMMIT_SHA
    ).pattern
    assert pattern == handler_pattern


# --- the tag ----------------------------------------------------------------


def test_equal_inputs_give_equal_tags(index):
    a = index.image_tag(index.build_inputs(_properties()))
    b = index.image_tag(index.build_inputs(_properties()))
    assert a == b
    assert re.fullmatch(r"decider-[0-9a-f]{24}", a)


@pytest.mark.parametrize(
    "field, value",
    [
        ("SourceRevision", "0" * 40),
        ("ModelRevision", "1" * 40),
        ("ModelId", "StrandsAgents/other-model"),
        ("Vision", "false"),
    ],
)
def test_each_input_changes_the_tag(index, field, value):
    base = index.image_tag(index.build_inputs(_properties()))
    changed = index.image_tag(index.build_inputs(_properties(**{field: value})))
    assert changed != base


def test_the_recipe_version_changes_the_tag(index, monkeypatch):
    base = index.image_tag(index.build_inputs(_properties()))
    monkeypatch.setattr(index, "RECIPE_VERSION", "2")
    assert index.image_tag(index.build_inputs(_properties())) != base


def test_a_property_that_is_not_an_input_does_not_change_the_tag(index):
    base = index.image_tag(index.build_inputs(_properties()))
    other = index.image_tag(index.build_inputs(_properties(ProjectName="renamed")))
    assert other == base


# --- build or reuse ---------------------------------------------------------


def test_an_image_already_in_ecr_is_not_rebuilt(index):
    index.HELPER.Data = {}
    index.CODEBUILD.reset_mock()
    index.ECR.describe_images.side_effect = None
    index.ECR.describe_images.return_value = {"imageDetails": [{}]}
    event = _event()

    index.start_build(event, None)

    index.CODEBUILD.start_build.assert_not_called()
    tag = index.image_tag(index.build_inputs(event["ResourceProperties"]))
    assert index.HELPER.Data == {"ImageTag": tag}
    index.ECR.describe_images.assert_called_once_with(
        repositoryName="decider-repo", imageIds=[{"imageTag": tag}]
    )


def test_a_missing_image_is_built_with_the_inputs_that_name_it(index):
    index.HELPER.Data = {}
    index.CODEBUILD.reset_mock()
    index.ECR.describe_images.side_effect = _ImageNotFound()
    index.CODEBUILD.start_build.return_value = {"build": {"id": "decider-build:1"}}
    event = _event()

    index.start_build(event, None)

    inputs = index.build_inputs(event["ResourceProperties"])
    tag = index.image_tag(inputs)
    kwargs = index.CODEBUILD.start_build.call_args.kwargs
    assert kwargs["projectName"] == "decider-build"
    passed = {v["name"]: v["value"] for v in kwargs["environmentVariablesOverride"]}
    assert passed == {**inputs, "IMAGE_TAG": tag}
    assert index.HELPER.Data == {"ImageTag": tag, "build_id": "decider-build:1"}


def test_a_refused_input_starts_no_build(index):
    index.HELPER.Data = {}
    index.CODEBUILD.reset_mock()
    with pytest.raises(ValueError):
        index.start_build(_event(SourceRevision="main"), None)
    index.CODEBUILD.start_build.assert_not_called()


# --- polling ----------------------------------------------------------------


def _poll_event(data: dict, physical_id: str | None = None) -> dict:
    event = {"RequestType": "Create", "CrHelperData": data}
    if physical_id:
        event["PhysicalResourceId"] = physical_id
    return event


def test_a_reused_image_completes_on_the_first_poll(index):
    index.CODEBUILD.reset_mock()
    assert index.poll_build(_poll_event({"ImageTag": "t"}), None) == (
        index.DEFAULT_PHYSICAL_ID
    )
    index.CODEBUILD.batch_get_builds.assert_not_called()


@pytest.mark.parametrize(
    "status, expected", [("SUCCEEDED", "pid"), ("IN_PROGRESS", None)]
)
def test_poll_follows_the_build(index, status, expected):
    index.CODEBUILD.batch_get_builds.return_value = {
        "builds": [{"buildStatus": status}]
    }
    event = _poll_event({"build_id": "b:1"}, physical_id="pid")
    assert index.poll_build(event, None) == expected


@pytest.mark.parametrize("status", ["FAILED", "FAULT", "STOPPED", "TIMED_OUT"])
def test_a_build_that_did_not_succeed_fails_the_resource(index, status):
    index.CODEBUILD.batch_get_builds.return_value = {
        "builds": [{"buildStatus": status}]
    }
    with pytest.raises(RuntimeError, match=f"ended {status}.*b:1"):
        index.poll_build(_poll_event({"build_id": "b:1"}), None)


# --- the template half ------------------------------------------------------


def test_the_custom_resource_passes_every_input(index, template):
    props = template["Resources"]["DeciderImageBuild"]["Properties"]
    assert props["SourceRevision"] == {"!Ref": "DeciderSourceRevision"}
    assert props["ModelId"] == {"!Ref": "ModelId"}
    assert props["ModelRevision"] == {"!Ref": "ModelRevision"}
    assert props["Vision"] == {"!Ref": "EnableVision"}
    assert props["ProjectName"] == {"!Ref": "DeciderImageBuildProject"}
    assert props["RepositoryName"] == {"!Ref": "ECRRepository"}


def _buildspec(template: dict) -> str:
    project = template["Resources"]["DeciderImageBuildProject"]["Properties"]
    assert project["Source"]["Type"] == "NO_SOURCE"
    return project["Source"]["BuildSpec"]


def test_the_buildspec_reads_every_variable_the_handler_sets(index, template):
    spec = _buildspec(template)
    names = set(index.build_inputs(_properties())) | {"IMAGE_TAG"}
    names -= {"RECIPE_VERSION"}  # part of the tag only
    for name in names:
        assert f"${name}" in spec or f"${{{name}}}" in spec or f" {name} " in spec, name
    # Every name the buildspec requires is one the handler or the project sets.
    required = re.search(r"for name in ([A-Z_ ]+); do", spec)
    assert required
    project_env = {
        v["Name"]
        for v in template["Resources"]["DeciderImageBuildProject"]["Properties"][
            "Environment"
        ]["EnvironmentVariables"]
    }
    assert set(required.group(1).split()) <= names | project_env


def _blocks(template: dict) -> list[str]:
    """Each phase's command block, as the shell will run it."""
    import yaml

    spec = yaml.safe_load(_buildspec(template))
    blocks = []
    for phase in ("pre_build", "build"):
        commands = spec["phases"][phase]["commands"]
        assert len(commands) == 1, f"{phase}: one block, so set -e covers every line"
        blocks.append(commands[0])
    return blocks


def _check_lines(block: str) -> list[tuple[str, str]]:
    """(``if`` line, the line after it) for every check in a block."""
    lines = [line.strip() for line in block.splitlines()]
    checks = []
    for i, line in enumerate(lines):
        if not line.startswith("if ") or "; then" not in line:
            continue
        head, _, rest = line.partition("; then")
        branch = rest.strip() or lines[i + 1]
        checks.append((head + "; then", branch.removesuffix("; fi").strip()))
    return checks


def test_every_block_stops_on_the_first_failure(template):
    for block in _blocks(template):
        assert block.splitlines()[0].strip() == "set -euo pipefail"


PIN_CHECK_LINES = {
    'if [ -z "${!name:-}" ]; then',
    'if ! [[ "${!name}" =~ ^[0-9a-f]{40}$ ]]; then',
    'if [ "$HUB_SHA" != "$MODEL_REVISION" ]; then',
    'if ! git cat-file -e "${SOURCE_REVISION}^{commit}" || [ -z "$(git for-each-ref '
    '--contains "$SOURCE_REVISION" refs/remotes/upstream refs/tags)" ]; then',
    'if [ "$(git rev-parse HEAD)" != "$SOURCE_REVISION" ]; then',
}


def test_every_check_fails_the_build(template):
    """Each pin check is present verbatim, and its branch ends in ``exit 1``.

    A check whose branch only prints is a log message, not a check.
    """
    checks = [c for block in _blocks(template) for c in _check_lines(block)]
    assert {line for line, _ in checks} == PIN_CHECK_LINES
    for line, branch in checks:
        assert branch.endswith("exit 1"), f"{line!r} does not exit: {branch!r}"


def test_the_source_is_fetched_from_the_fixed_repository_only(index, template):
    """Only the repository's own branches and tags are fetched.

    A pull request's head is served as ``refs/pull/*`` of the parent, so fetching
    any refspec beyond these two would let a fork's commit pass the check.
    """
    import shlex

    assert index.SOURCE_REPOSITORY == "https://github.com/strands-labs/strands-decider"
    build = _blocks(template)[1]
    assert 'git remote add upstream "$SOURCE_REPOSITORY"' in build
    fetches = [
        line.strip()
        for line in build.splitlines()
        if line.strip().startswith("git fetch")
    ]
    assert len(fetches) == 1
    assert shlex.split(fetches[0]) == [
        "git",
        "fetch",
        "-q",
        "--filter=blob:none",
        "upstream",
        "+refs/heads/*:refs/remotes/upstream/*",
        "+refs/tags/*:refs/tags/*",
    ]
    assert 'git checkout -q "$SOURCE_REVISION"' in build


def test_the_build_role_has_only_the_key_actions_it_uses(template):
    statements = template["Resources"]["DeciderImageBuildRole"]["Properties"][
        "Policies"
    ][0]["PolicyDocument"]["Statement"]
    (kms,) = [s for s in statements if str(s["Action"]).startswith("['kms:")]
    assert sorted(kms["Action"]) == [
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:GenerateDataKey*",
    ]


def test_the_hub_is_asked_about_the_pinned_revision(template):
    pre = _blocks(template)[0]
    assert (
        'curl -fsS "https://huggingface.co/api/models/${MODEL_ID}/revision/'
        '${MODEL_REVISION}"'
    ) in pre


def test_the_image_is_upstreams_recipe_with_exactly_the_pinned_arguments(template):
    import shlex

    build = _blocks(template)[1].replace("\\\n", " ")
    docker = [
        line for line in build.splitlines() if line.strip().startswith("docker build")
    ]
    assert len(docker) == 1
    argv = shlex.split(docker[0])
    build_args = [argv[i + 1] for i, a in enumerate(argv) if a == "--build-arg"]
    assert build_args == [
        "MODEL=$MODEL_ID",
        "MODEL_REVISION=$MODEL_REVISION",
        "VISION=$VISION",
    ]
    assert argv[argv.index("-f") + 1] == "deploy/lambda/Dockerfile"
    assert argv[argv.index("-t") + 1] == "$ECR_REPO_URI:$IMAGE_TAG"
    assert argv[-1] == "."
    assert 'docker push "$ECR_REPO_URI:$IMAGE_TAG"' in build


def test_the_build_finishes_inside_the_custom_resource_hour(template):
    minutes = template["Resources"]["DeciderImageBuildProject"]["Properties"][
        "TimeoutInMinutes"
    ]
    assert minutes < 60


def test_the_function_pulls_the_tag_the_custom_resource_returns(template):
    resources = template["Resources"]
    image_uri = resources["DeciderFunction"]["Properties"]["Code"]["ImageUri"]
    assert image_uri == {
        "!Sub": "${ECRRepository.RepositoryUri}:${DeciderImageBuild.ImageTag}"
    }
    version = resources["DeciderFunctionVersion"]
    # A new image, a SnapStart or tracing change, or a new release replaces the
    # version, so the alias moves; the old version and its snapshot are deleted.
    description = version["Properties"]["Description"]["!Sub"]
    for part in (
        "${DeciderImageBuild.ImageTag}",
        "${EnableSnapStart}",
        "${EnableXRayTracing}",
        "<FEATURE_VERSION_TOKEN>",
    ):
        assert part in description
    # Lambda publishes a version only when the function changed since the last
    # one; replacing the version on a description-only change fails the update.
    env = resources["DeciderFunction"]["Properties"]["Environment"]["Variables"]
    assert env["DECIDER_PUBLISHED_AS"] == version["Properties"]["Description"]
    assert version["DeletionPolicy"] == "Delete"
    assert version["UpdateReplacePolicy"] == "Delete"
    alias = resources["DeciderFunctionAlias"]["Properties"]
    assert alias["FunctionVersion"] == {"!GetAtt": "DeciderFunctionVersion.Version"}


def test_lambda_can_pull_a_new_image_after_create(template):
    """Lambda grants itself pull access only at create, not on an image update."""
    policy = template["Resources"]["ECRRepository"]["Properties"][
        "RepositoryPolicyText"
    ]
    (statement,) = policy["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"Service": {"!Sub": "lambda.${AWS::URLSuffix}"}}
    assert sorted(statement["Action"]) == [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    ]
    assert statement["Condition"]["StringLike"]["aws:sourceArn"] == {
        "!Sub": "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}"
        ":function:${DeciderFunctionName}*"
    }


def test_snapstart_is_off_unless_asked_for(template):
    assert template["Parameters"]["EnableSnapStart"]["Default"] == "false"
    assert template["Conditions"]["UseSnapStart"] == {
        "!Equals": [{"!Ref": "EnableSnapStart"}, "true"]
    }
    assert template["Resources"]["DeciderFunction"]["Properties"]["SnapStart"] == {
        "!If": [
            "UseSnapStart",
            {"ApplyOn": "PublishedVersions"},
            {"!Ref": "AWS::NoValue"},
        ]
    }


def test_vision_is_on_by_default_because_the_configuration_sends_images(template):
    assert template["Parameters"]["EnableVision"]["Default"] == "true"


def test_the_function_name_default_is_the_priced_name(template):
    pricing = (_FEATURE_DIR.parents[1] / "config_library" / "pricing.yaml").read_text(
        encoding="utf-8"
    )
    name = template["Parameters"]["DeciderFunctionName"]["Default"]
    assert f"name: decision_model/{name}\n" in pricing
    assert re.fullmatch(
        template["Parameters"]["DeciderFunctionName"]["AllowedPattern"], name
    )
