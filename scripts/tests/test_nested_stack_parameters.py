# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Parent/nested-stack parameter wiring, asserted against SOURCE templates.

cfn-lint has a rule for this (E3043, "Specified parameter doesn't exist in nested
stack template") but it cannot work in either environment we care about:

* In **CI** the parent's ``TemplateURL`` points at
  ``<dir>/.aws-sam/packaged.yaml`` — a *build* artifact absent from a fresh
  checkout. cfn-lint logs "Template file not found" and silently skips the check.
* In a **built local tree** the file exists but may predate a ``Parameters``
  change, so the rule reports false positives. Chasing five of those is what
  proved the rule was useless here.

So E3043 is disabled in ``make cfn-lint`` and this test does the job instead,
reading each nested stack's **source** ``template.yaml`` rather than its packaged
output. That is strictly better than the lint rule: it works on a clean checkout,
it cannot go stale, and it also catches the reverse direction (a required nested
parameter the parent never passes) which E3043 does not check at all.

Both failure modes are real deployment breakers:

* passing a parameter the nested template does not declare →
  ``Parameters: [X] do not exist in the template`` at CREATE/UPDATE time;
* omitting one the nested template requires (no ``Default``) →
  ``Parameters: [X] must have values``.

A **third** direction breaks nothing at deploy time and so is not covered by either
of those: a nested parameter that declares a ``Default`` the parent never overrides.
CloudFormation accepts it, and whatever the default decides is what ships — in the
two verified cases, a feature that silently self-disables. That is asserted at the
end of this module, against the recorded baseline
``nested_parameter_defaults_baseline.json``, because unlike the two above it is not a
defect in every instance. :mod:`nested_parameter_defaults` holds the scan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import nested_parameter_defaults
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PARENT_TEMPLATE = "template.yaml"


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load(rel_path: str) -> dict:
    return yaml.load((REPO_ROOT / rel_path).read_text(), Loader=_CfnLoader) or {}


def _source_template_for(template_url: Any) -> str | None:
    """Map a nested stack's TemplateURL to the SOURCE template it is built from.

    ``./feature-platform/main-stack-extensions/.aws-sam/packaged.yaml``
    -> ``feature-platform/main-stack-extensions/template.yaml``
    """
    if not isinstance(template_url, str):
        return None
    match = re.match(r"^\./(.+?)/\.aws-sam/packaged\.ya?ml$", template_url.strip())
    if not match:
        return None
    for candidate in (
        f"{match.group(1)}/template.yaml",
        f"{match.group(1)}/template.yml",
    ):
        if (REPO_ROOT / candidate).is_file():
            return candidate
    return None


def _nested_stacks() -> list[tuple[str, str]]:
    """(logical id, source template path) for every nested stack in the parent."""
    resources = _load(PARENT_TEMPLATE).get("Resources", {}) or {}
    found = []
    for name, body in resources.items():
        if not isinstance(body, dict):
            continue
        if body.get("Type") != "AWS::CloudFormation::Stack":
            continue
        source = _source_template_for((body.get("Properties") or {}).get("TemplateURL"))
        if source:
            found.append((name, source))
    return found


@pytest.mark.unit
def test_nested_stacks_are_discovered() -> None:
    """Guard the discovery itself — a silent zero would make the rest vacuous.

    This is the failure mode that made E3043 worthless: no templates resolved, no
    findings, green build.
    """
    stacks = _nested_stacks()
    assert len(stacks) >= 5, (
        f"expected at least 5 nested stacks in {PARENT_TEMPLATE}, found "
        f"{len(stacks)}: {stacks}. If TemplateURL no longer points at "
        f"'<dir>/.aws-sam/packaged.yaml', update _source_template_for()."
    )


@pytest.mark.unit
@pytest.mark.parametrize("logical_id,source", _nested_stacks(), ids=lambda v: str(v))
def test_parent_passes_only_parameters_the_nested_template_declares(
    logical_id: str, source: str
) -> None:
    """Passing an undeclared parameter fails the stack operation outright."""
    parent = _load(PARENT_TEMPLATE)
    passed = set(
        (
            (parent["Resources"][logical_id].get("Properties") or {}).get("Parameters")
            or {}
        )
    )
    declared = set(_load(source).get("Parameters", {}) or {})

    undeclared = sorted(passed - declared)
    assert not undeclared, (
        f"{PARENT_TEMPLATE} passes parameter(s) to {logical_id} that "
        f"{source} does not declare: {undeclared}. CloudFormation rejects this "
        f"with 'Parameters: [...] do not exist in the template'."
    )


@pytest.mark.unit
@pytest.mark.parametrize("logical_id,source", _nested_stacks(), ids=lambda v: str(v))
def test_parent_passes_every_required_nested_parameter(
    logical_id: str, source: str
) -> None:
    """A nested parameter with no Default must be supplied by the parent.

    E3043 does not check this direction at all.
    """
    parent = _load(PARENT_TEMPLATE)
    passed = set(
        (
            (parent["Resources"][logical_id].get("Properties") or {}).get("Parameters")
            or {}
        )
    )
    declared = _load(source).get("Parameters", {}) or {}
    required = {
        name
        for name, spec in declared.items()
        if isinstance(spec, dict) and "Default" not in spec
    }

    missing = sorted(required - passed)
    assert not missing, (
        f"{source} requires parameter(s) with no Default that "
        f"{PARENT_TEMPLATE} does not pass to {logical_id}: {missing}. "
        f"CloudFormation rejects this with 'Parameters: [...] must have values'."
    )


# --------------------------------------------------------------------------- #
# The third direction: a Default the parent never overrides
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_unpassed_default_scan_agrees_with_this_module_s_own_parse() -> None:
    """The finding set re-derived here, through this module's parse of the templates.

    Asserted as an **equality**, in both directions, because each direction fails
    differently and the gate needs both. A scan reporting something that is not a
    finding puts a name in the baseline that answers for nothing; a scan reporting
    only *some* of them leaves the rest invisible while every check downstream passes
    — the silent-zero shape that made E3043 worthless, one step short of zero.

    The derivation below is deliberately a second one, off this module's own loader
    and resolver rather than ``gate_premises``'s. Two parses of the same templates
    disagreeing is a cheap thing to detect and the only thing standing between the
    baseline and an unexamined set.
    """
    stacks = dict(_nested_stacks())
    parent = _load(PARENT_TEMPLATE)
    expected = set()
    for logical_id, source in stacks.items():
        given = (
            (parent["Resources"][logical_id].get("Properties") or {}).get("Parameters")
        ) or {}
        for name, spec in (_load(source).get("Parameters") or {}).items():
            if isinstance(spec, dict) and "Default" in spec and name not in given:
                expected.add(f"{logical_id}::{name}")

    findings = nested_parameter_defaults.findings()
    assert {item.key for item in findings} == expected, (
        "nested_parameter_defaults.findings() and this module's own parse of the same "
        f"templates disagree.\n  only the scan reports: "
        f"{sorted({i.key for i in findings} - expected)}\n  only this test reports: "
        f"{sorted(expected - {i.key for i in findings})}"
    )
    assert findings, (
        "the scan produced no findings at all. Either every nested parameter is now "
        "passed or defaulted-and-passed -- in which case delete the baseline and this "
        "check -- or template resolution broke. Compare against "
        f"{sorted(stacks)}."
    )

    problems = []
    for item in findings:
        if item.stack not in stacks:
            problems.append(f"{item.key}: {item.stack} is not a nested stack here")
            continue
        if stacks[item.stack] != item.template:
            problems.append(
                f"{item.key}: scan says {item.template}, this module resolves "
                f"{stacks[item.stack]}"
            )
            continue
        spec = (_load(item.template).get("Parameters") or {}).get(item.parameter)
        if not isinstance(spec, dict) or "Default" not in spec:
            problems.append(f"{item.key}: not declared with a Default in its template")
        passed = (
            (parent["Resources"][item.stack].get("Properties") or {}).get("Parameters")
        ) or {}
        if item.parameter in passed:
            problems.append(f"{item.key}: the parent does pass it, at that resource")
    assert not problems, (
        "nested_parameter_defaults.findings() disagrees with this module's own parse "
        f"of the same templates: {problems}"
    )


@pytest.mark.unit
def test_a_parameter_group_does_not_count_as_reading_a_parameter() -> None:
    """``referenced`` is the field that makes the baseline more than a list of names.

    Four things it has to get right, and the synthetic template exercises all four:
    a long-form ``Ref`` and the short form this repo's loader rewrites to ``Fn::Ref``,
    a ``${Name}`` inside ``Fn::Sub``, the ``${!Literal}`` escape that is not a
    reference, and a name appearing only in an ``AWS::CloudFormation::Interface``
    parameter group -- which is what a textual search would count and is the reason
    this scan is structural.
    """
    template = {
        "Parameters": {
            name: {"Type": "String"} for name in ("Long", "Short", "Subbed")
        },
        "Conditions": {"HasLong": {"Fn::Equals": [{"Ref": "Long"}, ""]}},
        "Resources": {
            "Thing": {
                "Properties": {
                    "A": {"Fn::Ref": "Short"},
                    "B": {"Fn::Sub": "prefix-${Subbed}-${!NotAReference}-suffix"},
                    "C": {"Fn::Sub": ["${AlsoSubbed}", {"K": "v"}]},
                    "D": {"Fn::GetAtt": ["NotAParameter", "Arn"]},
                }
            }
        },
        "Metadata": {
            "AWS::CloudFormation::Interface": {
                "ParameterGroups": [{"Parameters": ["GroupedOnly"]}]
            }
        },
    }
    found = nested_parameter_defaults.referenced_names(template)
    assert found == {"Long", "Short", "Subbed", "AlsoSubbed"}, (
        f"referenced_names returned {sorted(found)}. 'GroupedOnly' appears only in a "
        "console parameter group, 'NotAReference' is the ${!x} escape, and "
        "'NotAParameter' is a GetAtt target -- none is a reference."
    )


@pytest.mark.unit
def test_no_unaccounted_defaulted_parameter_the_parent_never_passes() -> None:
    """The universe closure: a finding must be in one of the baseline's two sections.

    This is the check the whole file exists for. A nested parameter with a ``Default``
    the parent never overrides breaks no deploy and logs nothing, so the only way it
    becomes visible is for the set to be recorded and compared.
    """
    baseline = nested_parameter_defaults.load_baseline()
    unaccounted, _, _ = nested_parameter_defaults.classify(
        nested_parameter_defaults.findings(), baseline
    )
    assert not unaccounted, (
        "these nested-stack parameter(s) declare a Default that "
        f"{PARENT_TEMPLATE} never overrides, and are in neither section of "
        "scripts/tests/nested_parameter_defaults_baseline.json:\n  "
        + "\n  ".join(
            f"{item.key}  ({item.template}, "
            + ("read by that template" if item.referenced else "not read by it")
            + ")"
            for item in unaccounted
        )
        + f"\n\nPass the parameter from {PARENT_TEMPLATE} -- that is the fix in every "
        "verified case. If the nested default genuinely is the intended value, record "
        "it with\n  python3 scripts/tests/nested_parameter_defaults.py --write "
        '--allow-new-unpassed "why the default is correct here"\nand if it is a real '
        "defect, add it to knownDefects by hand with the issue that tracks it."
    )


@pytest.mark.unit
def test_every_recorded_entry_is_still_a_finding() -> None:
    """A recorded entry whose parameter the parent now passes must be delisted.

    The baseline key carries no line number and no value, so a dead entry does not go
    inert -- it pre-accepts the next parameter to take that name in that stack. This
    is the same reason ``scripts/srt/issues.json`` fails on a suppression that shields
    nothing.
    """
    baseline = nested_parameter_defaults.load_baseline()
    _, fixed, _ = nested_parameter_defaults.classify(
        nested_parameter_defaults.findings(), baseline
    )
    assert not fixed, (
        f"these entries are recorded in the baseline but {PARENT_TEMPLATE} now passes "
        f"the parameter (or it is gone): {fixed}. Delete them -- `python3 "
        "scripts/tests/nested_parameter_defaults.py --write` does it -- because an "
        "entry with no live finding silently accepts whatever next takes that name."
    )


@pytest.mark.unit
def test_no_recorded_parameter_has_become_read_by_its_nested_template() -> None:
    """A default that decided nothing and now decides something is a new finding.

    Recorded as ``referenced: false`` means the nested template declares the parameter
    and reads it nowhere, so the default cannot affect behaviour. Wiring it to a
    condition or a property changes the entry's whole standing without changing the
    list of names, which is why this is asserted separately.
    """
    baseline = nested_parameter_defaults.load_baseline()
    _, _, newly_read = nested_parameter_defaults.classify(
        nested_parameter_defaults.findings(), baseline
    )
    assert not newly_read, (
        "these entries are recorded as not read by their nested template and are now "
        f"read by it, so the unpassed default has started deciding behaviour: "
        f"{[item.key for item in newly_read]}. Pass the parameter from "
        f"{PARENT_TEMPLATE}, or re-record with --allow-new-unpassed and say why the "
        "default is right."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    sorted(
        nested_parameter_defaults.recorded(nested_parameter_defaults.load_baseline())
    ),
)
def test_each_baseline_entry_answers_for_itself(key: str) -> None:
    """One entry, one member's worth of reason -- and the two sections stay distinct.

    A single justification covering a set is the defect
    ``scripts/tests/gate_exemptions.json`` exists to prevent, and it is the obvious
    way for this baseline to rot: every name sharing one sentence about defaults
    usually being deliberate.
    """
    baseline = nested_parameter_defaults.load_baseline()
    agreed = baseline.get("accepted") or {}
    defects = baseline.get("knownDefects") or {}

    overlap = sorted(set(agreed) & set(defects))
    assert not overlap, (
        f"{overlap} are recorded as both agreed-to and a known defect. An entry is one "
        "or the other; that is the only judgement this file carries."
    )

    if key in defects:
        entry = defects[key]
        assert entry.get("defect", "").strip(), (
            f"{key}: a knownDefects entry must say what silently stops working. The "
            "whole reason the section exists is that the gate is green on a tree that "
            "still has the defect."
        )
        assert isinstance(entry.get("issues"), list), (
            f"{key}: 'issues' must be a list of issue numbers, empty if nobody has "
            "filed it yet. Absent is indistinguishable from unconsidered."
        )
    else:
        entry = agreed[key]
        assert entry.get("reason", "").strip(), (
            f"{key}: an agreed entry needs a reason stating a fact about THIS tree -- "
            "what reads the parameter, what the default means to it, and why the "
            f"parent having no parameter for it is a decision."
        )
    assert isinstance(entry.get("referenced"), bool), (
        f"{key}: 'referenced' must be recorded, because it is what "
        "test_no_recorded_parameter_has_become_read_by_its_nested_template compares."
    )
    assert entry.get("template", "").strip(), f"{key}: 'template' is missing"


@pytest.mark.unit
def test_write_refuses_to_record_a_new_unpassed_parameter_without_a_reason(
    tmp_path: Path,
) -> None:
    """``--write`` must not be able to launder a finding into the baseline.

    Without this refusal the loop is: the gate reddens on a new silently-defaulted
    parameter and says 'record it or pass it', somebody runs ``--write``, and the
    parameter is accepted for good with no reason attached. That is the failure
    ``scripts/check_lint_debt.py`` guards the same way, and the reason
    ``--allow-new-unpassed`` takes a string rather than being a bare flag.
    """
    live = nested_parameter_defaults.findings()
    assert live, "no findings to drive the refusal with"
    victim = live[0]

    baseline = nested_parameter_defaults.load_baseline()
    for section in ("accepted", "knownDefects"):
        baseline.get(section, {}).pop(victim.key, None)
    copy = tmp_path / "baseline.json"
    copy.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    before = copy.read_text(encoding="utf-8")

    assert nested_parameter_defaults.write(path=copy) == 1, (
        f"--write accepted {victim.key} with no reason given"
    )
    assert copy.read_text(encoding="utf-8") == before, (
        "--write refused and still modified the baseline"
    )

    assert nested_parameter_defaults.write("measured in a test", path=copy) == 0
    written = json.loads(copy.read_text(encoding="utf-8"))
    assert written["accepted"][victim.key]["reason"] == "measured in a test"
    assert "measured in a test" in written["newEntryJustifications"]
    assert victim.key not in written["knownDefects"], (
        "--write must never write into knownDefects: that section is a judgement with "
        "an issue behind it, not something a regeneration can decide."
    )


@pytest.mark.unit
def test_the_command_line_report_agrees_with_the_gate() -> None:
    """The CLI is the way this is read by hand, so it must not drift from the tests.

    ``report()`` returns non-zero on exactly the three disagreements the three checks
    above assert separately. If it returned 0 unconditionally it would be the thing
    people trust while the suite is what actually runs.
    """
    assert nested_parameter_defaults.report() == 0, (
        "the CLI reports a disagreement the tests above did not catch, or vice versa "
        "-- run `python3 scripts/tests/nested_parameter_defaults.py` for the detail"
    )
