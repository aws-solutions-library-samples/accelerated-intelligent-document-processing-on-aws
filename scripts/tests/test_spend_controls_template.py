# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``MaxConcurrentWorkflows`` ceiling (GitHub issue #934, item 5).

Item 5 is a one-line parameter constraint whose **value** is the whole risk.
CloudFormation validates parameter constraints against the value an
``update-stack`` submits, including the previous value reused under
``UsePreviousValue``, so a ceiling below a live deployment's current setting
makes that stack's next update unsubmittable. The ceiling is therefore asserted
against values this repository itself uses rather than against a number written
down twice, and against a floor that keeps it above the account-level Lambda
concurrency limit — a ceiling that could bind before Lambda does would be a
capacity opinion rather than the typo guard it is meant to be.

Item 2's ``DocumentEstimatedCost`` metric is covered in
``lib/idp_common_pkg/tests/unit/reporting/test_document_cost_metric.py``, where
the emitting code lives. The last test here is about neither item: it asserts
that **no spend-enforcement path exists**, because item 3 of that issue — the
circuit breaker wired to a spend alarm — is an open product decision and the
metric is to reach the maintainer before anything acts on it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = REPO / "template.yaml"

CONCURRENCY_PARAM = "MaxConcurrentWorkflows"

# Above this, the ceiling cannot be the first thing a growing deployment hits:
# AWS Lambda's default account-level concurrent-execution limit is 1000, and one
# workflow holds several concurrent invocations, so Lambda binds long before a
# ceiling at or above this does. A ``MaxValue`` below it would be a capacity
# opinion rather than a typo guard, and an opinion is the thing that wedges an
# update.
CEILING_FLOOR = 1000


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags.

    Subclasses ``SafeLoader``, so the ``python/object`` constructors that make
    ``yaml.load`` dangerous are never registered, and the multi-constructor below
    only ever returns plain scalars, lists and dicts.
    """


def _tag_to_python(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> dict:
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load(path: Path) -> dict:
    loader = _CfnLoader(path.read_text(encoding="utf-8"))
    try:
        return loader.get_single_data() or {}
    finally:
        loader.dispose()


@pytest.fixture(scope="module")
def template() -> dict:
    return _load(MAIN_TEMPLATE)


def _blob(node: Any) -> str:
    """A node as sorted JSON, for substring checks that do not depend on the
    loader's spelling of a short-form tag."""
    return json.dumps(node, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# Item 5: the MaxConcurrentWorkflows ceiling                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_max_concurrent_workflows_has_a_ceiling(template):
    param = template["Parameters"][CONCURRENCY_PARAM]
    assert "MaxValue" in param, (
        f"{CONCURRENCY_PARAM} has no MaxValue, so a slipped digit (100000 for "
        f"100) is accepted and becomes the deployment's spend rate"
    )
    assert param["MinValue"] == 1, "the existing floor must not move"


@pytest.mark.unit
def test_the_shipped_default_is_still_accepted(template):
    """The other direction, and the one that breaks a deployment rather than
    merely failing to guard it. A ceiling below the ``Default`` makes a
    *create*-stack with no parameter overrides unsubmittable."""
    param = template["Parameters"][CONCURRENCY_PARAM]
    default = int(param["Default"])
    assert param["MinValue"] <= default <= param["MaxValue"], (
        f"Default={default} falls outside [{param['MinValue']}, "
        f"{param['MaxValue']}] — every deployment that does not override this "
        f"parameter would be rejected at submit time"
    )


@pytest.mark.unit
def test_no_value_this_repository_uses_exceeds_the_ceiling(template):
    """Derived from the tree, not written down twice.

    A ``MaxValue`` below a value a live stack is running makes that stack's next
    ``update-stack`` fail validation — including when the operator passes nothing
    and CloudFormation reuses the previous value. Nothing in this repository can
    see a customer's deployed value, so the best available proxy is every value
    the repository itself configures, deploys or documents. If a future change
    lowers the ceiling under one of them, that is the signal.
    """
    pattern = re.compile(
        rf"(?:{CONCURRENCY_PARAM}|max_concurrent)[\"']?\s*[:=]\s*[\"']?(\d+)"
    )
    ceiling = int(template["Parameters"][CONCURRENCY_PARAM]["MaxValue"])
    offenders = []
    found_any = False
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in {
            ".py",
            ".yaml",
            ".yml",
            ".json",
            ".md",
            ".sh",
        }:
            continue
        rel = path.relative_to(REPO).as_posix()
        if (
            rel.startswith(("node_modules/", ".git/", "build/"))
            or "/node_modules/" in rel
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for value in pattern.findall(text):
            found_any = True
            if int(value) > ceiling:
                offenders.append(f"{rel}: {value}")
    assert found_any, (
        "the scan found no concurrency value anywhere, so this assertion is "
        "vacuous — the naming convention it matches must have changed"
    )
    assert not offenders, (
        f"MaxValue={ceiling} is below a value this repository already uses, so "
        f"an update-stack on a deployment at that value fails validation: "
        f"{offenders}"
    )


@pytest.mark.unit
def test_the_ceiling_is_a_typo_guard_not_a_capacity_limit(template):
    """Why the ceiling is set so far above anything plausible.

    The guard's purpose is to reject a slipped digit — ``100000`` typed for
    ``100`` — at submit time. It is not a view on how much concurrency a
    deployment should have, and it must not become one, because every ceiling is
    a future ``update-stack`` validation failure for whoever sits above it. That
    asymmetry is the reason a ceiling is right here and would be wrong on a
    parameter where it guards nothing.
    """
    ceiling = int(template["Parameters"][CONCURRENCY_PARAM]["MaxValue"])
    assert ceiling >= CEILING_FLOOR, (
        f"MaxValue={ceiling} is at or below the account-level Lambda "
        f"concurrency limit, so this constraint could become the binding "
        f"constraint on a real workload instead of just rejecting a slipped digit"
    )


# --------------------------------------------------------------------------- #
# Item 3 is NOT built                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_no_spend_enforcement_path_exists(template):
    """Item 3 of #934 — the circuit breaker wired to a spend alarm — is an open
    product decision, and the decision recorded on the issue is that the metric
    is delivered first and that enforcement, when it comes, throttles rather than
    halts. So this change must not have created one by accident: no alarm may
    watch a cost metric.
    """
    cost_metric_names = {"DocumentEstimatedCost", "UnpricedMeteringRows"}
    offenders = []
    for name, res in template["Resources"].items():
        if not isinstance(res, dict) or res.get("Type") != "AWS::CloudWatch::Alarm":
            continue
        if any(m in _blob(res) for m in cost_metric_names):
            offenders.append(name)
    assert not offenders, (
        f"alarms watching the cost metric: {offenders}. Item 3 of #934 is not "
        f"authorized — the metric goes to the maintainer before anything acts on "
        f"it."
    )
