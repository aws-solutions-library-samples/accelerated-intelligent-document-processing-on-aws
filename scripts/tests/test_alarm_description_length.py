# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every ``AlarmDescription`` must fit CloudWatch's 1024-character limit when resolved.

CloudWatch rejects an `AlarmDescription` longer than 1024 characters with a 400, and
CloudFormation surfaces that as a `CREATE_FAILED` on the alarm — so the whole stack
fails to deploy. Nothing catches it earlier: `cfn-lint` has no length rule for this
property, and the descriptions in this repository are operator runbooks that grow a
paragraph at a time as new causes are discovered, which is exactly how one crosses the
limit without anyone noticing.

That is not hypothetical. `DocumentQueueStalledAlarm` reached **1350** resolved
characters by gaining one paragraph about the circuit breaker's state-read failure, and
the first thing to notice was a stack create failing at the alarm.

The length that matters is the **resolved** one, so `${AWS::StackName}` is substituted
with the longest name a stack can carry. The 25-character cap is not arbitrary: the
`IsStacknameLengthOK` custom resource in `template.yaml` fails the stack operation above
it, so no deployable stack has a longer name. Other `${...}` substitutions are replaced
with a generous placeholder, since their real values are parameters this test cannot
resolve — that keeps the check conservative rather than optimistic.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

#: CloudWatch's documented maximum for the property.
MAX_ALARM_DESCRIPTION = 1024

#: The longest stack name a deployable stack can have — enforced at deploy time by the
#: ``IsStacknameLengthOK`` custom resource, so substituting it is the worst real case.
MAX_STACK_NAME_LEN = 25

#: Stand-in for any other ``${Parameter}`` reference. Long enough that a description
#: which only fits because a parameter happens to be short still fails here.
_OTHER_SUB_PLACEHOLDER = "x" * 40

_SUB_RE = re.compile(r"\$\{([^}]+)\}")


class _CfnLoader(yaml.SafeLoader):
    """CloudFormation short-form tags are not valid YAML tags; keep the scalar."""


def _passthrough(loader, _tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnLoader.add_multi_constructor("!", _passthrough)


def _templates() -> list[Path]:
    """Every template git tracks that declares an alarm description.

    Discovery is by **content**, so a new template cannot be added without being
    covered — the convention `make cfn-lint` and `make check-arn-partitions` already
    use. It reads only files **git tracks**, which matters for two reasons: build
    output vendors whole copies of templates, and agent worktrees under `.claude/`
    contain other branches' copies of this very file. Walking the filesystem instead
    makes the result depend on what the machine happens to have on disk — findings CI
    could never reproduce, which is the defect this kind of gate is supposed to catch
    rather than commit.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*.yml", "*.yaml"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = []
    for rel in (p for p in out.split("\0") if p):
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "AWSTemplateFormatVersion" in text and "AlarmDescription" in text:
            found.append(path)
    return found


def _resolved_len(value: str) -> int:
    """The length after substituting the worst realistic value for each ``${...}``."""

    def replace(match: re.Match[str]) -> str:
        ref = match.group(1)
        if ref == "AWS::StackName":
            return "x" * MAX_STACK_NAME_LEN
        if ref.startswith("AWS::"):
            # Region, account id, partition: all shorter than the placeholder, but use
            # it anyway rather than guessing a specific deployment.
            return _OTHER_SUB_PLACEHOLDER
        return _OTHER_SUB_PLACEHOLDER

    return len(_SUB_RE.sub(replace, value))


def _alarm_descriptions() -> list[tuple[Path, str, str]]:
    out: list[tuple[Path, str, str]] = []
    for path in _templates():
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_CfnLoader)
        for logical_id, resource in (doc.get("Resources") or {}).items():
            if not isinstance(resource, dict):
                continue
            if resource.get("Type") != "AWS::CloudWatch::Alarm":
                continue
            desc = (resource.get("Properties") or {}).get("AlarmDescription")
            if isinstance(desc, str):
                out.append((path, logical_id, desc))
    return out


@pytest.mark.unit
def test_alarms_are_discovered():
    """A vacuous pass here would hide every over-long description."""
    found = _alarm_descriptions()
    assert len(found) >= 10, (
        "discovery found only "
        f"{len(found)} AlarmDescription(s), which is fewer than this repository has — "
        "the CloudFormation tag loader or the content-based template discovery has "
        "broken, and this file is now checking almost nothing."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "path,logical_id,description",
    [(p, lid, d) for p, lid, d in _alarm_descriptions()],
    ids=[f"{lid}" for _, lid, _ in _alarm_descriptions()],
)
def test_alarm_description_fits_the_cloudwatch_limit(
    path: Path, logical_id: str, description: str
):
    resolved = _resolved_len(description)
    assert resolved <= MAX_ALARM_DESCRIPTION, (
        f"{path.relative_to(REPO_ROOT)}: {logical_id}'s AlarmDescription resolves to "
        f"{resolved} characters, over CloudWatch's {MAX_ALARM_DESCRIPTION} limit, so "
        "the alarm returns a 400 and the stack fails to create. These descriptions are "
        "operator runbooks and grow a paragraph at a time; move the detail to "
        "docs/monitoring.md and leave the description naming the causes and their "
        "signals."
    )
