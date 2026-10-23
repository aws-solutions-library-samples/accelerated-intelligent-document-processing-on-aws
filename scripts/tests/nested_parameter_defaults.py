# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Nested-stack parameters that declare a ``Default`` the parent never overrides.

``scripts/tests/test_nested_stack_parameters.py`` asserts the two wiring errors
CloudFormation itself rejects: a parameter the nested template does not declare, and
a parameter with **no** ``Default`` that the parent fails to supply. A defaulted
parameter the parent never passes is outside both by construction, and it is the
quiet one — the stack deploys, nothing logs, and whatever the default decides is
what ships.

Two live instances were verified by hand, and they are the shape worth describing
because neither is visible to any other check in this tree:

* ``nested/api-resolvers/template.yaml`` declares ``MLflowLoggerFunctionArn`` with
  ``Default: ""``, derives a condition from it, and puts both an environment
  variable and a ``lambda:InvokeFunction`` grant behind that condition. The parent
  passes nothing, so the condition is permanently false and the resolver returns
  immediately on every test run. The sibling parameter declared beside it *is*
  passed. Issue 1553.
* ``nested/bedrockkb/template.yaml`` declares ``pEmbedModel`` with three
  ``AllowedValues`` and a Titan default. The parent passes nine other parameters to
  the same resource and not this one, so the two Cohere embedding models that
  ``docs/knowledge-base.md`` lists as supported cannot be selected at all.

**Reported against a baseline, not failed outright.** Most of these are deliberate:
an optional knob a deployment is expected to leave alone, or a value the solution
has exactly one sensible setting for. A check that failed on all of them would be
turned off within a day. So the current set is recorded in
``nested_parameter_defaults_baseline.json`` with a per-member reason, and the gate
fails only when the tree and the baseline disagree — in either direction.

``referenced`` is the field that keeps the baseline from being a list of names. A
parameter the nested template never reads is dead weight; one it *reads* is a
default silently deciding behaviour. A recorded entry that goes from unread to read
has become the dangerous kind, and :func:`classify` reports that as a change
requiring a decision even though the parameter list did not move.

The scan is structural rather than textual, which is what makes that distinction
worth anything: a plain ``grep`` for a parameter name in a nested template hits the
``AWS::CloudFormation::Interface`` parameter groups in its ``Metadata``, where names
appear as bare strings for the console's benefit, and every grouped parameter would
read as used. Only ``Ref`` and ``Fn::Sub`` count here, so those groups contribute
nothing in either case; the template's top-level ``Metadata`` is skipped as well,
because CloudFormation does not evaluate intrinsics there, so a ``Ref`` written
inside it is not a reference either. Measured over this tree, the skip changes no
verdict today — every parameter in a parameter group is also genuinely read — and
it is the structural rule, not the skip, that keeps ``referenced`` informative.

Run ``python3 scripts/tests/nested_parameter_defaults.py`` to see the current
disagreement, ``--summary`` for the counts, and ``--write`` to re-record. ``--write``
removes entries whose parameter the parent now passes and will **not** add one, or
record a parameter becoming read, without ``--allow-new-unpassed "<reason>"``;
without that refusal it would be a laundering step for the exact defect the gate
exists to report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gate_premises

REPO_ROOT = gate_premises.REPO_ROOT
BASELINE = Path(__file__).resolve().parent / "nested_parameter_defaults_baseline.json"

#: ``${Name}`` inside an ``Fn::Sub`` body. ``${!Literal}`` is an escape and is not a
#: reference, which is why ``!`` is excluded from the first character.
_SUB_TOKEN = re.compile(r"\$\{([^{}!][^{}]*)\}")


@dataclass(frozen=True)
class Finding:
    """One nested parameter that declares a ``Default`` the parent never overrides."""

    #: Logical id of the ``AWS::CloudFormation::Stack`` resource in the parent.
    stack: str
    #: Repo-relative path of the nested stack's SOURCE template.
    template: str
    #: Parameter name as the nested template declares it.
    parameter: str
    #: Whether the nested template reads the parameter outside its ``Metadata``.
    referenced: bool

    @property
    def key(self) -> str:
        """The baseline key: ``<logical id>::<parameter>``."""
        return f"{self.stack}::{self.parameter}"


def referenced_names(template: dict) -> set[str]:
    """Names the template reads through ``Ref`` or ``Fn::Sub``, excluding Metadata.

    ``Fn::GetAtt`` is not read: it names a resource, never a parameter. A
    ``${Resource.Attr}`` token inside an ``Fn::Sub`` contributes its leading segment,
    which can only over-report — and over-reporting marks a finding as the dangerous
    kind, which is the safe direction for this scan to be wrong in.
    """
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("Ref", "Fn::Ref") and isinstance(value, str):
                    names.add(value)
                elif key in ("Sub", "Fn::Sub"):
                    body = value[0] if isinstance(value, list) and value else value
                    if isinstance(body, str):
                        names.update(
                            match.group(1).split(".")[0].strip()
                            for match in _SUB_TOKEN.finditer(body)
                        )
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk({key: value for key, value in template.items() if key != "Metadata"})
    return names


def findings() -> tuple[Finding, ...]:
    """Every defaulted nested parameter the parent does not pass, sorted by key.

    The parent-to-nested-template relation comes from
    ``gate_premises.nested_stack_sources()``, which reads each ``TemplateURL`` back to
    the **source** template rather than the packaged build artifact it points at.
    """
    parent = gate_premises.load_template(gate_premises.PARENT_TEMPLATE)
    resources = parent.get("Resources") or {}
    found: list[Finding] = []
    for stack, source in sorted(gate_premises.nested_stack_sources().items()):
        properties = ((resources.get(stack) or {}).get("Properties")) or {}
        passed = set(properties.get("Parameters") or {})
        nested = gate_premises.load_template(source)
        declared = nested.get("Parameters") or {}
        read = referenced_names(nested)
        for name, spec in sorted(declared.items()):
            if not isinstance(spec, dict) or "Default" not in spec:
                continue
            if name in passed:
                continue
            found.append(Finding(stack, source, name, name in read))
    return tuple(sorted(found, key=lambda item: item.key))


def load_baseline(path: Path | None = None) -> dict:
    return json.loads((path or BASELINE).read_text(encoding="utf-8"))


def recorded(baseline: dict) -> dict[str, dict]:
    """Every baseline entry from both sections, keyed the way a finding is.

    Flattened on purpose: which section an entry sits in is a judgement about it, and
    every mechanical check below — is it still live, has it become read — applies the
    same way to both.
    """
    merged: dict[str, dict] = {}
    for section in ("accepted", "knownDefects"):
        for key, entry in (baseline.get(section) or {}).items():
            merged[key] = {**entry, "section": section}
    return merged


def classify(
    live: tuple[Finding, ...], baseline: dict
) -> tuple[list[Finding], list[str], list[Finding]]:
    """``(unaccounted, fixed, newly_read)`` between the tree and the baseline.

    * ``unaccounted`` — a finding in **neither** section. This is the universe closure:
      a new silently-defaulted parameter cannot be in the tree without being here.
    * ``fixed`` — a recorded key the scan no longer produces, because the parent now
      passes it or the parameter is gone. A dead entry pre-accepts whatever next takes
      that name, so it is a failure rather than a courtesy.
    * ``newly_read`` — recorded as unread by the nested template and now read by it.
    """
    known = recorded(baseline)
    by_key = {item.key: item for item in live}
    unaccounted = [item for item in live if item.key not in known]
    fixed = sorted(key for key in known if key not in by_key)
    newly_read = [
        item
        for item in live
        if item.key in known
        and item.referenced
        and not known[item.key].get("referenced", False)
    ]
    return unaccounted, fixed, newly_read


def _entry(item: Finding, reason: str) -> dict:
    return {
        "template": item.template,
        "referenced": item.referenced,
        "reason": reason,
    }


def write(allow_new_unpassed: str | None = None, path: Path | None = None) -> int:
    """Re-record the baseline. Tightening is automatic; loosening needs a reason."""
    path = path or BASELINE
    baseline = load_baseline(path)
    live = findings()
    unaccounted, fixed, newly_read = classify(live, baseline)

    if (unaccounted or newly_read) and not allow_new_unpassed:
        detail = (
            "\n".join(
                f"   + {item.key}  ({item.template}, "
                f"{'read by the nested template' if item.referenced else 'declared but not read'})"
                for item in unaccounted
            )
            or "   (none)"
        )
        promoted = (
            "\n".join(
                f"   ! {item.key}  recorded as not read, now read by {item.template}"
                for item in newly_read
            )
            or "   (none)"
        )
        print(
            "refusing to write: this would record a parameter the parent does not "
            "pass as agreed-to.\n"
            f"newly unaccounted for:\n{detail}\n"
            f"recorded as unread and now read:\n{promoted}\n\n"
            "   Pass the parameter from template.yaml instead, which is the fix in\n"
            "   every case so far. If the default genuinely is the intended value,\n"
            "   re-run with:\n\n"
            '       --write --allow-new-unpassed "why the default is correct here"\n\n'
            "   which records the reason in the baseline so it is reviewable rather\n"
            "   than invisible. A real defect belongs in the knownDefects section,\n"
            "   by hand, with the issue that tracks it.",
            file=sys.stderr,
        )
        return 1

    for section in ("accepted", "knownDefects"):
        entries = baseline.setdefault(section, {})
        for key in fixed:
            entries.pop(key, None)
        for item in live:
            if item.key in entries:
                entries[item.key]["template"] = item.template
                entries[item.key]["referenced"] = item.referenced

    if allow_new_unpassed:
        for item in unaccounted:
            baseline.setdefault("accepted", {})[item.key] = _entry(
                item, allow_new_unpassed
            )
        baseline.setdefault("newEntryJustifications", []).append(allow_new_unpassed)

    for section in ("accepted", "knownDefects"):
        baseline[section] = dict(sorted(baseline[section].items()))
    path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    shown = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
    print(
        f"recorded {len(baseline['accepted'])} agreed and "
        f"{len(baseline['knownDefects'])} known-defect entr(ies) into "
        f"{shown}; removed {len(fixed)}: {fixed or 'none'}"
    )
    return 0


def report() -> int:
    """Print the disagreement between the tree and the baseline. 0 means none."""
    baseline = load_baseline()
    live = findings()
    unaccounted, fixed, newly_read = classify(live, baseline)
    print(
        f"{len(live)} defaulted nested parameter(s) the parent does not pass, over "
        f"{len(gate_premises.nested_stack_sources())} nested stack(s)."
    )
    for item in unaccounted:
        print(f"  unaccounted for: {item.key}  ({item.template})")
    for key in fixed:
        print(f"  recorded but no longer a finding: {key}")
    for item in newly_read:
        print(f"  recorded as unread, now read: {item.key}")
    return 1 if (unaccounted or fixed or newly_read) else 0


def summary() -> int:
    baseline = load_baseline()
    live = findings()
    print(f"live findings:        {len(live)}")
    print(f"  read by the nested template:  {sum(1 for i in live if i.referenced)}")
    print(f"recorded as agreed:   {len(baseline.get('accepted') or {})}")
    print(f"recorded as defects:  {len(baseline.get('knownDefects') or {})}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="re-record the baseline from the current tree",
    )
    parser.add_argument(
        "--allow-new-unpassed",
        metavar="REASON",
        help=(
            "permit --write to record a parameter the parent does not pass, or one "
            "that has become read by its nested template. The reason is stored in "
            "the baseline."
        ),
    )
    parser.add_argument("--summary", action="store_true", help="print the counts only")
    args = parser.parse_args(argv)
    if args.write:
        return write(args.allow_new_unpassed)
    if args.summary:
        return summary()
    return report()


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
