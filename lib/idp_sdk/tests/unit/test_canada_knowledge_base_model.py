# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A Canada stack left at the Knowledge Base model default gets a model it can call.

``KnowledgeBaseModelId`` defaults to ``us.amazon.nova-pro-v1:0``, which has no
inference profile in ca-central-1, so ``RetrieveAndGenerate`` there fails with
``ResourceNotFoundException: The specified inference profile ... does not
exist``. The root template passes the Canada geo profile of Nova Lite,
``ca.amazon.nova-lite-v1:0``, to the API stack instead, but only when the stack
runs in a Canada region AND the parameter is still at its default. An explicit
choice always passes through unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from idp_sdk._core.template_transform import HeadlessTemplateTransformer

pytestmark = pytest.mark.unit

CONDITION = "UseCanadaKnowledgeBaseModel"
CANADA_MODEL = "ca.amazon.nova-lite-v1:0"
DEFAULT_MODEL = "us.amazon.nova-pro-v1:0"


class _CFNLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short intrinsic tags."""


def _cfn_multi_constructor(loader, tag_suffix, node):
    tag = "!" + tag_suffix
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag: value}


_CFNLoader.add_multi_constructor("!", _cfn_multi_constructor)


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


@pytest.fixture(scope="module")
def template() -> dict:
    with open(_repo_root() / "template.yaml", "r", encoding="utf-8") as f:
        # nosec B506 - _CFNLoader subclasses yaml.SafeLoader; the only
        # customization is a no-op constructor for CFN intrinsic tags. Input is
        # this repo's own committed template.
        return yaml.load(f, Loader=_CFNLoader)  # nosec B506


def _holds(template: dict, node: Any, values: dict) -> bool:
    """Evaluate a condition expression; ``values`` supplies every ``!Ref``."""
    ((tag, args),) = node.items()
    if tag == "!Condition":
        return _holds(template, template["Conditions"][args], values)
    if tag == "!Equals":
        left, right = (
            values[arg["!Ref"]] if isinstance(arg, dict) else arg for arg in args
        )
        return left == right
    if tag == "!And":
        return all(_holds(template, arg, values) for arg in args)
    if tag == "!Or":
        return any(_holds(template, arg, values) for arg in args)
    raise AssertionError(f"unhandled condition expression {tag}: {node!r}")


def test_the_api_stack_receives_the_conditional_model(template):
    value = template["Resources"]["APIRESOLVERSTACK"]["Properties"]["Parameters"][
        "KnowledgeBaseModelId"
    ]
    assert value == {
        "!If": [CONDITION, CANADA_MODEL, {"!Ref": "KnowledgeBaseModelId"}]
    }, f"the API stack must get its model through {CONDITION}, got {value!r}"


def test_the_condition_switches_from_the_parameter_default(template):
    parameter = template["Parameters"]["KnowledgeBaseModelId"]
    assert parameter["Default"] == DEFAULT_MODEL, (
        f"{CONDITION} compares against the default, so it must change with it"
    )
    assert CANADA_MODEL in parameter["AllowedValues"], (
        "an operator must also be able to choose the Canada model explicitly"
    )


@pytest.mark.parametrize(
    "region,chosen,expected",
    [
        ("ca-central-1", DEFAULT_MODEL, CANADA_MODEL),
        ("ca-west-1", DEFAULT_MODEL, CANADA_MODEL),
        # An explicit choice is honoured, in Canada too.
        ("ca-central-1", "us.amazon.nova-2-lite-v1:0", "us.amazon.nova-2-lite-v1:0"),
        # Every other region keeps the parameter value.
        ("us-east-1", DEFAULT_MODEL, DEFAULT_MODEL),
        ("eu-central-1", DEFAULT_MODEL, DEFAULT_MODEL),
        ("us-gov-west-1", "amazon.nova-pro-v1:0", "amazon.nova-pro-v1:0"),
    ],
)
def test_only_a_canada_stack_at_the_default_is_switched(
    template, region, chosen, expected
):
    switched = _holds(
        template,
        {"!Condition": CONDITION},
        {"AWS::Region": region, "KnowledgeBaseModelId": chosen},
    )
    assert (CANADA_MODEL if switched else chosen) == expected


def test_the_headless_transform_drops_the_condition():
    """Headless removes KnowledgeBaseModelId and APIRESOLVERSTACK, so the condition
    must go too: CloudFormation rejects a template whose Condition references a
    deleted parameter."""
    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")

    def _plain(node):
        if isinstance(node, dict):
            return {str(k): _plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_plain(x) for x in node]
        if isinstance(node, str):
            return str(node)
        return node

    loaded = cfnlint_decode.load(str(_repo_root() / "template.yaml"))
    base = _plain(loaded[0] if isinstance(loaded, tuple) else loaded)
    assert isinstance(base, dict), "the template should load as a mapping"
    assert CONDITION in base["Conditions"], "premise: the root template declares it"

    result = HeadlessTemplateTransformer().apply_transforms(base)
    assert "KnowledgeBaseModelId" not in result.get("Parameters", {})
    assert CONDITION not in result.get("Conditions", {})
