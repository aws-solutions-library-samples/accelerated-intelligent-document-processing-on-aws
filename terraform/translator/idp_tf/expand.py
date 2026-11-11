"""Load a CloudFormation template and expand the SAM transform locally.

The input is the template ``publish.py`` has packaged, so every ``CodeUri``,
``ContentUri`` and ``DefinitionUri`` is already an ``s3://`` URI and no build
step is needed here.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import re
from pathlib import Path
from typing import Any, Iterator

import boto3
from samtranslator.parser.parser import Parser  # type: ignore[import-untyped]
from samtranslator.translator.arn_generator import (  # type: ignore[import-untyped]
    ArnGenerator,
)
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


#: The partition SAM is told it is translating for. See :func:`_sam_pinned`.
SAM_PARTITION = "aws"

#: Literals SAM writes for the partition it was given and for the partition's
#: DNS suffix (service principals such as ``lambda.amazonaws.com``). Each is
#: rewritten to the pseudo-parameter CloudFormation would resolve it from, so
#: the generated module names no partition. ``.amazonaws.com.cn`` is China's
#: suffix itself and is never written by SAM for the pinned partition.
_PARTITION_LITERAL = "arn:aws:"
_URL_SUFFIX_LITERAL = re.compile(r"\.amazonaws\.com(?!\.cn)\b")

#: SAM leaves a parameter whose value has this prefix unresolved; it is how
#: CloudFormation passes a value that does not exist yet. Giving the region and
#: partition pseudo-parameters such a value keeps every ``Ref``/``${}`` to them
#: a reference instead of a value SAM read from its session.
_UNRESOLVED = "{{IntrinsicFunction:idp-tf/"


def _strings(value: Any) -> set[str]:
    """Every string leaf of a template, keys excluded."""
    if isinstance(value, dict):
        return set().union(*(_strings(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_strings(v) for v in value)) if value else set()
    return {value} if isinstance(value, str) else set()


def _names_a_partition(text: str) -> bool:
    return _PARTITION_LITERAL in text or bool(_URL_SUFFIX_LITERAL.search(text))


def _pseudo(text: str) -> str:
    text = text.replace(_PARTITION_LITERAL, "arn:${AWS::Partition}:")
    return _URL_SUFFIX_LITERAL.sub(".${AWS::URLSuffix}", text)


def _partition_independent(value: Any, authored: set[str]) -> Any:
    """Rewrite every partition literal SAM wrote into a pseudo-parameter.

    SAM writes ``arn:aws:`` into managed-policy ARNs, API Gateway integration
    URIs and permission source ARNs, and ``amazonaws.com`` into service
    principals. A string the template author wrote is left alone, whatever it
    holds: ``make check-arn-partitions`` already rules on those, and its
    exceptions (a content security policy, a Cognito host) are values that
    must stay literal. Inside ``Fn::Sub`` the literal becomes a ``${}``
    reference; a plain string becomes an ``Fn::Sub`` with any ``${`` in it
    escaped.
    """
    if isinstance(value, list):
        return [_partition_independent(v, authored) for v in value]
    if isinstance(value, dict):
        if len(value) == 1 and "Fn::Sub" in value:
            arg = value["Fn::Sub"]
            if isinstance(arg, str):
                if arg in authored or not _names_a_partition(arg):
                    return value
                return {"Fn::Sub": _pseudo(arg)}
            if isinstance(arg, list) and arg and isinstance(arg[0], str):
                text = arg[0]
                if text not in authored and _names_a_partition(text):
                    text = _pseudo(text)
                rest = [_partition_independent(v, authored) for v in arg[1:]]
                return {"Fn::Sub": [text, *rest]}
        return {k: _partition_independent(v, authored) for k, v in value.items()}
    if isinstance(value, str) and value not in authored and _names_a_partition(value):
        return {"Fn::Sub": _pseudo(value.replace("${", "${!"))}
    return value


def _refuse_partition_dependent_defaults(template: dict[str, Any]) -> None:
    """SAM decisions that depend on the partition and cannot be rewritten.

    With no ``EndpointConfiguration`` SAM gives a REST API an EDGE endpoint in
    ``aws`` and a REGIONAL one in GovCloud and China, so the pinned partition
    would choose EDGE for every partition.
    """
    for lid, res in (template.get("Resources") or {}).items():
        if res.get("Type") == "AWS::Serverless::Api":
            props = res.get("Properties") or {}
            if "EndpointConfiguration" not in props:
                raise ValueError(
                    f"{lid}: an AWS::Serverless::Api must declare "
                    "EndpointConfiguration; SAM's default differs by partition"
                )


@contextlib.contextmanager
def _sam_pinned() -> Iterator[None]:
    """Make every partition decision SAM takes the same, whatever its session.

    SAM reads the partition from its session region, and acts on it in three
    ways: it writes it into ARNs (rewritten afterwards by
    :func:`_partition_independent`), it picks managed policies from its
    per-partition table, and it changes defaults (refused by
    :func:`_refuse_partition_dependent_defaults`). Pinning it to ``aws`` fixes
    the second, which includes the X-Ray policy: ``AWSXrayWriteOnlyAccess`` in
    ``aws``, ``AWSXRayDaemonWriteAccess`` elsewhere. ``AWSXrayWriteOnlyAccess``
    is in SAM's table for every partition and is the one the explicit roles in
    ``patterns/unified`` and the feature-platform extensions attach, so the
    module carries that one everywhere; both grant ``xray:PutTraceSegments``.
    """
    original = ArnGenerator.__dict__["get_partition_name"]
    ArnGenerator.get_partition_name = classmethod(  # type: ignore[method-assign]
        lambda cls, region=None: SAM_PARTITION
    )
    try:
        yield
    finally:
        ArnGenerator.get_partition_name = original  # type: ignore[method-assign]


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
    _refuse_partition_dependent_defaults(template)
    # SAM refuses to run without a session region, so it is given a fixed one;
    # nothing in the output depends on which (see _sam_pinned and the
    # pseudo-parameter values below), and translator tests assert that across
    # commercial and GovCloud regions. CI's hermetic runs have no region.
    # The rest is samtranslator.translator.transform.transform(), which takes
    # no session.
    source = copy.deepcopy(template)
    authored = _strings(_normalise(template))
    parameters: dict[str, Any] = {
        "AWS::Region": _UNRESOLVED + "AWS::Region}}",
        "AWS::Partition": _UNRESOLVED + "AWS::Partition}}",
    }
    to_py27_compatible_template(source, parameters)
    with _sam_pinned():
        expanded = undo_mark_unicode_str_in_template(
            Translator(
                None,
                Parser(),
                boto_session=boto3.session.Session(region_name=SAM_REGION),
            ).translate(
                source,
                parameter_values=parameters,
                get_managed_policy_map=_NoManagedPolicies().load,
            )
        )
    expanded.pop("Transform", None)
    leaked = {s for s in _strings(expanded) if _UNRESOLVED in s}
    if leaked:
        raise ValueError(f"SAM resolved a pseudo-parameter to a placeholder: {leaked}")
    return _sam_shape_fixups(_partition_independent(_normalise(expanded), authored))
