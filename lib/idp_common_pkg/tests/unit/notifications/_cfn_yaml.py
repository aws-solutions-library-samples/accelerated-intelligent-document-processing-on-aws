# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation-tag-tolerant YAML loader shared by template parity tests."""

from pathlib import Path
from typing import Any

import yaml

PKG_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = PKG_ROOT.parents[1]


def load_cfn_yaml(path: Path) -> Any:
    """Load a CloudFormation template, turning intrinsic tags into plain dicts.

    A SafeLoader subclass with a catch-all constructor for ``!Ref``, ``!Sub``,
    ``!GetAtt``, ``!If`` and the rest, so a template can be read without
    resolving anything.
    """

    class _CFNLoader(yaml.SafeLoader):
        pass

    def _cfn(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
        tag = "!" + tag_suffix
        if isinstance(node, yaml.ScalarNode):
            return {tag: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {tag: loader.construct_sequence(node)}
        return {tag: loader.construct_mapping(node)}  # type: ignore[arg-type]

    _CFNLoader.add_multi_constructor("!", _cfn)
    with open(path, "r", encoding="utf-8") as f:
        # nosec B506 - _CFNLoader subclasses yaml.SafeLoader and only adds a
        # no-op constructor for CFN intrinsic tags; input is this repo's own
        # committed template.
        return yaml.load(f, Loader=_CFNLoader)  # nosec B506
