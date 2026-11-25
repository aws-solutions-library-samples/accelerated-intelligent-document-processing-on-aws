# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review-queue routing runs with only the extras the Pipeline ProcessResults installs.

That Lambda installs ``idp_common_pkg[core,docs_service,review_queues]``. Routing
once reached ``idp_common.assessment`` for the class-schema lookup, whose package
``__init__`` loads the assessment service and Pillow, so every Pipeline-mode
document failed with ``No module named 'PIL'`` as soon as the Default queue was
enabled. The unit suite installs every extra, so nothing else here could see it.

The child process refuses to import any distribution that only an extra the Lambda
does not install brings in, then drives the routing path that turns a section's
confidence alerts into reasons and looks up its class schema. Both lists are read from the files the
build reads, so a new extra or a changed requirements line is covered unchanged.
"""

import json
import os
import re
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = _PKG_ROOT.parents[1]
_REQUIREMENTS = (
    _REPO_ROOT
    / "patterns"
    / "unified"
    / "src"
    / "processresults_function"
    / "requirements.txt"
)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _dist(requirement: str) -> str:
    match = _NAME.match(requirement.strip())
    assert match, requirement
    return match.group(0).lower().replace("_", "-")


def _installed_extras() -> list[str]:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    match = re.search(r"idp_common_pkg\[([^\]]+)\]", text)
    assert match, f"{_REQUIREMENTS} no longer installs idp_common_pkg with extras"
    return [extra.strip() for extra in match.group(1).split(",")]


def _blocked_modules() -> list[str]:
    """Top-level modules of distributions the Lambda does not install."""
    project = tomllib.loads((_PKG_ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    installed = {_dist(r) for r in project["dependencies"]}
    for extra in _installed_extras():
        installed |= {_dist(r) for r in extras[extra]}
    elsewhere = {
        _dist(r)
        for name, reqs in extras.items()
        if name not in _installed_extras()
        for r in reqs
        if not r.startswith("idp_common")
    } - installed
    modules = set()
    for module, dists in packages_distributions().items():
        if {d.lower().replace("_", "-") for d in dists} & elsewhere:
            modules.add(module)
    return sorted(m for m in modules if m.isidentifier())


_CHILD = r"""
import importlib.abc
import json
import sys

BLOCKED = set(json.loads(sys.argv[1]))


class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named '{name}'")
        return None


sys.meta_path.insert(0, Refuse())

from types import SimpleNamespace

from idp_common.review_queues.reasons import build_reasons

section = SimpleNamespace(
    section_id="1",
    classification="W2",
    confidence_threshold_alerts=[
        {"attribute_name": "Employer", "confidence": 0.4, "confidence_threshold": 0.9}
    ],
)
config = SimpleNamespace(classes=[{"x-aws-idp-document-type": "W2", "properties": {}}])
reasons = build_reasons(SimpleNamespace(sections=[section]), config, mode="pipeline")
import idp_common.review_queues.routing  # noqa: F401

# Evaluating a routing table needs no XML parser: the DMN reader and writer are
# imported only by export and import.
from idp_common.config.routing_models import BUILTIN_CONFIDENCE_REASON, RoutingTable
from idp_common.decision_tables import evaluate
from idp_common.review_queues.routing_dmn import to_decision_table

decision = evaluate(
    to_decision_table(RoutingTable()),
    {"reviewed": False, "confidence_alert_count": len(reasons)},
)
assert decision.outputs == {
    "outcome": "queue",
    "queue": "default",
    "reason": BUILTIN_CONFIDENCE_REASON,
}, decision

# The routing step's inputs: built from the document and the section results,
# with no assessment or rule validation code.
from idp_common.review_queues.routing_inputs import build_inputs


class Results:
    def get_json_content(self, uri):
        return {"inference_result": {}, "explainability_info": [{}]}


section.extraction_result_uri = "s3://out/doc/sections/1/result.json"
section.excluded = False
inputs = build_inputs(
    SimpleNamespace(sections=[section], num_pages=1, rule_validation_result=None),
    config,
    RoutingTable(),
    mode="pipeline",
    s3=Results(),
)
assert inputs.values["confidence_alert_count"] == 1, inputs
assert inputs.values["reviewed"] is False, inputs
assert evaluate(to_decision_table(RoutingTable()), inputs.values).outputs == {
    "outcome": "queue",
    "queue": "default",
    "reason": BUILTIN_CONFIDENCE_REASON,
}
# What a reviewer reads: the reason of a routing rule, and the policy rules the
# document failed, read from the summary with no rule validation code.
import idp_common.config.routing_reason  # noqa: F401
from idp_common.review_queues.routing_inputs import InputReader


class Summary:
    def get_json_content(self, uri):
        return {
            "rule_details": {
                "lending": {
                    "rules": [
                        {
                            "rule_id": "lending.max_ltv",
                            "verdict": "fail",
                            "reasoning": "LTV is over the limit.",
                            "supporting_pages": ["2"],
                        }
                    ]
                }
            }
        }


failures, failure_count = InputReader(
    SimpleNamespace(
        sections=[section],
        num_pages=1,
        rule_validation_result=SimpleNamespace(
            output_uri="s3://out/doc/rule_validation/consolidated/summary.md"
        ),
    ),
    config,
    "pipeline",
    Summary(),
).failed_policy_rules(limit=10, reason_bytes=500, max_pages=10)
assert failure_count == 1, failure_count
assert [(f.policy, f.name, f.pages) for f in failures] == [
    ("lending", "max_ltv", (2,))
], failures
# The routing step itself: the decision, and what it needs to place a document.
from idp_common.review_queues.routing_step import decide, evaluate_table

assert evaluate_table(RoutingTable(), inputs.values).matched_rule_id == (
    "builtin-confidence-alerts"
)
assert decide(
    SimpleNamespace(sections=[section], submission_source="test-studio"),
    config,
    mode="pipeline",
    execution_arn="arn:aws:states:us-east-1:111122223333:execution:sm:run",
    run_plan=None,
    store=None,
).reason == "test_studio"
# Applying a completed review runs in the same image.
from idp_common.review_queues.apply_review import review_record

assert review_record(SimpleNamespace(cycle_id="c", disposed_by="r", disposed_at="t"))[
    "confirmed"
]
# So does applying it to a section the run extracted again, which reads where
# the review-queue service kept the result the reviewer started from.
from idp_common.review_queues.apply_review import _base_uri

assert _base_uri("out", "doc.pdf", "c", "1") == "s3://out/doc.pdf/review/base/c/1.json"
assert "idp_common.review_queues.service" in sys.modules
assert not [
    name
    for name in sys.modules
    if name.startswith(("idp_common.assessment", "idp_common.rule_validation"))
], "routing loaded assessment or rule validation code"
assert "defusedxml" not in sys.modules
assert "idp_common.decision_tables.dmn" not in sys.modules
print(json.dumps([r.field for r in reasons]))
"""


@pytest.mark.unit
class TestRoutingImportWeight:
    def test_the_blocked_set_covers_pillow(self):
        # Non-vacuous: the regression this guards was Pillow, from the image extra.
        assert "PIL" in _blocked_modules()

    def test_routing_reads_confidences_without_the_extras_the_lambda_lacks(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        run = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-c", _CHILD, json.dumps(_blocked_modules())],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout.strip().splitlines()[-1]) == ["Employer"]
