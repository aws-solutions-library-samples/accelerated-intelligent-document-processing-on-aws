# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A fit report's refs resolve in this release's solution profile.

``fit-report.json`` names limitations as ``limitation:<id>``, and the contract says
each is an id in the profile of the version that ran. The fit check's refs are
constants (``idp_sdk._core.fit_check.refs``), so the check is made here, against
``solution-profile/`` in the same tree: every constant must name an item that
exists, and every recorded report under the SDK's fixtures must validate against
the report schema with every ref it carries resolving. ``make solution-profile-check``
then guards the items themselves.
"""

import ast
import json
from pathlib import Path

import pytest
import yaml

from idp_sdk._core.fit_check import refs as fit_refs
from idp_sdk._core.fit_check.report import build_report, validate_report

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "solution-profile"
FIXTURES = REPO_ROOT / "lib/idp_sdk/tests/unit/fixtures/fit_check"
FIT_CHECK_PKG = REPO_ROOT / "lib/idp_sdk/idp_sdk/_core/fit_check"


def _profile_refs() -> set[str]:
    found = set()
    for kind, file, key in (
        ("limitation", "limitations.yaml", "limitations"),
        ("capability", "capabilities.yaml", "capabilities"),
    ):
        data = yaml.safe_load((PROFILE / file).read_text(encoding="utf-8"))
        found |= {f"{kind}:{item['id']}" for item in data[key]}
    return found


def _report_refs(report: dict) -> set[str]:
    out = {r for d in report["documents"] for r in d["triggered_limitations"]}
    return out | {t["ref"] for t in report["summary"]["triggered_limitations"]}


@pytest.mark.unit
@pytest.mark.parametrize("ref", fit_refs.ALL_REFS)
def test_every_ref_the_fit_check_can_report_is_in_the_profile(ref):
    assert ref in _profile_refs(), (
        f"{ref} is not in solution-profile/; add the item there (make "
        "solution-profile-check then guards it) rather than reporting an invented id"
    )


@pytest.mark.unit
def test_no_ref_is_spelled_outside_the_refs_module():
    """A literal 'limitation:...' in a detector would bypass the test above."""
    offenders = []
    for path in sorted(FIT_CHECK_PKG.glob("*.py")):
        if path.name == "refs.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(("limitation:", "capability:")):
                    offenders.append(f"{path.name}: {node.value}")
    assert not offenders, offenders


@pytest.mark.unit
def test_the_fit_check_is_itself_in_the_profile():
    assert "capability:fit-check" in _profile_refs()


RECORDS = sorted(FIXTURES.glob("*run-record.json"))


@pytest.mark.unit
def test_there_is_a_recorded_run_to_check():
    assert RECORDS, f"no recorded run under {FIXTURES}"


@pytest.mark.unit
@pytest.mark.parametrize("record_path", RECORDS, ids=lambda p: p.name)
def test_a_recorded_report_validates_and_its_refs_resolve(record_path):
    report = build_report(json.loads(record_path.read_text(encoding="utf-8")))
    validate_report(report)
    unresolved = _report_refs(report) - _profile_refs()
    assert not unresolved, f"{record_path.name}: refs not in the profile: {unresolved}"
