"""Load a CloudFormation template and expand the SAM transform locally.

The input is the template ``publish.py`` has packaged, so every ``CodeUri``,
``ContentUri`` and ``DefinitionUri`` is already an ``s3://`` URI and no build
step is needed here.
"""

from __future__ import annotations

import copy
import datetime
from pathlib import Path
from typing import Any

from samtranslator.translator.transform import transform  # type: ignore[import-untyped]
from samtranslator.yaml_helper import yaml_parse  # type: ignore[import-untyped]

SAM_TRANSFORM = "AWS::Serverless-2016-10-31"


class _NoManagedPolicies:
    """SAM asks for the account's managed-policy map; ARNs are built instead."""

    def load(self) -> dict[str, str]:
        return {}


def _normalise(value: Any) -> Any:
    """YAML reads ``2012-10-17`` as a date; CloudFormation reads it as text."""
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _partition_safe(value: Any) -> Any:
    """SAM writes ``arn:aws:`` for AWS managed policies; make it partition-safe."""
    if isinstance(value, dict):
        return {k: _partition_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_partition_safe(v) for v in value]
    if isinstance(value, str) and value.startswith("arn:aws:iam::aws:policy/"):
        suffix = value[len("arn:aws:") :]
        return {"Fn::Sub": "arn:${AWS::Partition}:" + suffix}
    return value


def load_template(path: Path) -> dict[str, Any]:
    template = yaml_parse(path.read_text())
    return _normalise(template)


def expand(template: dict[str, Any]) -> dict[str, Any]:
    """Return a plain CloudFormation template with the SAM transform applied."""
    transforms = template.get("Transform")
    if isinstance(transforms, str):
        transforms = [transforms]
    transforms = list(transforms or [])
    if not transforms:
        return copy.deepcopy(template)
    unsupported = [t for t in transforms if t != SAM_TRANSFORM]
    if unsupported:
        raise ValueError(f"unsupported template transform(s): {unsupported}")
    expanded = transform(copy.deepcopy(template), {}, _NoManagedPolicies())
    expanded.pop("Transform", None)
    return _partition_safe(_normalise(expanded))
