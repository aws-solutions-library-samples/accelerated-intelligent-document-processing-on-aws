# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every Lambda that can call a decision model or a LambdaHook may invoke one.

Both are reached through the classification, extraction and assessment services:
a section labelled by a decision model, a value scored by one, or any of those
calls configured as ``model: LambdaHook``. The client invokes a ``GENAIIDP-``
function directly, so a function that runs those services without
``lambda:InvokeFunction`` on ``GENAIIDP-*`` accepts the configuration and then
fails every document with ``AccessDeniedException``.

That happened: the Advanced-extraction shard runtime runs its confidence pass in
process and had no grant, while the five functions beside it did (GitHub #1656).
The set of functions checked is therefore **derived** from the sources, not
listed: any function whose code uses one of those services must carry the grant,
so the next function to run them is checked without anyone remembering to add it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PATTERN = REPO / "patterns" / "unified"
TEMPLATE = PATTERN / "template.yaml"

#: The services whose calls can go to a decision model or a LambdaHook function.
SERVICES = re.compile(
    r"\b(ClassificationService|AssessmentService|ExtractionService)\b"
)
IMAGE_TAG = re.compile(r":([a-z0-9-]+)-\$\{ImageVersion\}")
GRANT_RESOURCE = "function:GENAIIDP-*"


class _CfnLoader(yaml.SafeLoader):
    """Reads CloudFormation's short-form tags as plain values."""


def _any_tag(loader, _suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnLoader.add_multi_constructor("!", _any_tag)


def _functions():
    resources = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)[
        "Resources"
    ]
    return {
        name: body.get("Properties", {})
        for name, body in resources.items()
        if body.get("Type") == "AWS::Serverless::Function"
    }


def _source_dir(props) -> Path | None:
    if props.get("CodeUri"):
        return (PATTERN / str(props["CodeUri"])).resolve()
    match = IMAGE_TAG.search(str(props.get("ImageUri", "")))
    if not match:
        return None
    tag = match.group(1)
    for name in (tag.replace("-", "_"), tag):
        candidate = PATTERN / "src" / name
        if candidate.is_dir():
            return candidate
    return None


def _uses_services(source: Path) -> bool:
    return any(
        SERVICES.search(path.read_text(encoding="utf-8", errors="ignore"))
        for path in source.glob("*.py")
        if not path.name.startswith("test")
    )


def _has_grant(props) -> bool:
    for policy in props.get("Policies") or []:
        if not isinstance(policy, dict):
            continue
        for statement in policy.get("Statement") or []:
            if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
                continue
            actions = statement.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            resources = statement.get("Resource")
            resources = resources if isinstance(resources, list) else [resources]
            if "lambda:InvokeFunction" in actions and any(
                GRANT_RESOURCE in str(r) for r in resources
            ):
                return True
    return False


def _reaching() -> dict:
    out = {}
    for name, props in _functions().items():
        source = _source_dir(props)
        if source is not None and _uses_services(source):
            out[name] = props
    return out


def test_the_derived_set_is_not_empty_and_includes_the_shard_runtime():
    """Non-vacuity, and the case that motivated the check."""
    reaching = _reaching()
    assert {
        "ClassificationFunction",
        "AssessmentFunction",
        "ExtractionFunction",
    } <= set(reaching), sorted(reaching)
    assert "ShardRuntimeFunction" in reaching, sorted(reaching)


@pytest.mark.parametrize("name", sorted(_reaching()))
def test_a_function_that_runs_the_services_may_invoke_genaiidp_functions(name):
    assert _has_grant(_reaching()[name]), (
        f"{name} runs the classification, extraction or assessment service, which "
        "can call a decision model or a LambdaHook function, but has no "
        "lambda:InvokeFunction on GENAIIDP-*: every such call would be refused"
    )
