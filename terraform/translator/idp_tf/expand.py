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

import boto3
from samtranslator.parser.parser import Parser  # type: ignore[import-untyped]
from samtranslator.translator.translator import (  # type: ignore[import-untyped]
    Translator,
)
from samtranslator.utils.py27hash_fix import (  # type: ignore[import-untyped]
    to_py27_compatible_template,
    undo_mark_unicode_str_in_template,
)
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


def _sam_shape_fixups(template: dict[str, Any]) -> dict[str, Any]:
    """Shapes SAM passes through that the target resource type defines
    differently. Each fixup names one property of one type."""
    for res in template.get("Resources", {}).values():
        if res.get("Type") == "AWS::StepFunctions::StateMachine":
            logging = (res.get("Properties") or {}).get("LoggingConfiguration") or {}
            for dest in logging.get("Destinations", []) or []:
                group = dest.get("CloudWatchLogsLogGroup")
                # SAM accepts the log group ARN directly; the resource type
                # wants {"LogGroupArn": ...}.
                if group is not None and not (
                    isinstance(group, dict) and "LogGroupArn" in group
                ):
                    dest["CloudWatchLogsLogGroup"] = {"LogGroupArn": group}
    return template


OFFLINE_BUCKET = "offline-placeholder"


def offline_placeholders(template: dict[str, Any]) -> dict[str, Any]:
    """Point local code paths at a placeholder S3 location.

    The per-pull-request gate translates the source templates, before
    ``publish.py`` has packaged them, so ``CodeUri``, ``DefinitionUri`` and a
    local ``Code`` path are paths in the checkout. SAM refuses those; the
    generated module only needs a well-formed S3 location to validate.
    """
    out = copy.deepcopy(template)
    for res in out.get("Resources", {}).values():
        props = res.get("Properties") or {}
        rtype = res.get("Type", "")
        for key in ("CodeUri", "ContentUri", "DefinitionUri"):
            v = props.get(key)
            if isinstance(v, str) and not v.startswith("s3://"):
                props[key] = f"s3://{OFFLINE_BUCKET}/{v.strip('./')}"
        if rtype == "AWS::Lambda::Function" and isinstance(props.get("Code"), str):
            props["Code"] = {"S3Bucket": OFFLINE_BUCKET, "S3Key": props["Code"]}
    return out


def load_template(path: Path) -> dict[str, Any]:
    template = yaml_parse(path.read_text())
    return _normalise(template)


#: The region SAM's session is given; see :func:`expand`.
SAM_REGION = "us-east-1"


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
    # SAM fills AWS::Region and AWS::Partition from a boto3 session and refuses
    # to run without a region. The output carries those pseudo-parameters as
    # references, not values (measured: identical across us-east-1, us-west-2
    # and us-gov-west-1), so a fixed region makes the translation independent of
    # the environment; CI's hermetic runs have none.
    # The rest is samtranslator.translator.transform.transform(), which takes
    # no session.
    source = copy.deepcopy(template)
    parameters: dict[str, Any] = {}
    to_py27_compatible_template(source, parameters)
    expanded = undo_mark_unicode_str_in_template(
        Translator(
            None, Parser(), boto_session=boto3.session.Session(region_name=SAM_REGION)
        ).translate(
            source,
            parameter_values=parameters,
            get_managed_policy_map=_NoManagedPolicies().load,
        )
    )
    expanded.pop("Transform", None)
    return _sam_shape_fixups(_partition_safe(_normalise(expanded)))
