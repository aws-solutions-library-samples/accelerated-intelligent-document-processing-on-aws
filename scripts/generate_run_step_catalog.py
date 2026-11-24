#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Write the pipeline step list for the surfaces that cannot import ``idp_common``.

``lib/idp_common_pkg/idp_common/run_plan/steps.py`` declares, once, the steps a
run can start at or skip, and the display steps a run does that it can do
neither with. The SDK and the CLI read that module. Two surfaces cannot:

* the web UI, which is TypeScript;
* ``src/lambda/agentcore_gateway_manager/``, the custom resource that registers
  the MCP tool schema, which is packaged without the ``idp_common`` layer.

So the list is GENERATED into each of them as an identical JSON file and
committed. ``scripts/tests/test_run_step_catalog.py`` rebuilds the catalogue on
every test run and fails when a committed copy differs, and it holds the SDK, CLI,
gateway and UI to the same set of step values. Run this script when it fails:

    python3 scripts/generate_run_step_catalog.py           # rewrite the copies
    python3 scripts/generate_run_step_catalog.py --check   # CI-style check
    python3 scripts/generate_run_step_catalog.py --print   # stdout only

The web UI's routing rule editor has the same need for a second list: what a
routing condition can test (``ROUTING_INPUTS`` in
``idp_common/config/routing_validation.py``) and the limits a rule is held to.
That is written to the UI as ``routing-inputs.json`` by the same run and checked
by the same ``--check``.

Both sources are read from this checkout by path, never through
``import idp_common``: an installed copy can belong to another tree, and a
generated file has to describe the code it is committed beside. The step module
imports the standard library only and is loaded; the routing module imports the
configuration models, so its constants are read from its source instead.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
STEPS_MODULE = Path("lib/idp_common_pkg/idp_common/run_plan/steps.py")

#: Every committed copy of the catalogue. All are byte-identical.
TARGETS = (
    Path("src/ui/src/components/run-plan/run-steps.json"),
    Path("src/lambda/agentcore_gateway_manager/run_steps.json"),
)

ROUTING_MODULE = Path("lib/idp_common_pkg/idp_common/config/routing_validation.py")

#: The committed copy of the routing input catalogue.
ROUTING_INPUT_TARGETS = (
    Path("src/ui/src/components/config-routing/routing-inputs.json"),
)

#: The limits and names of the routing module that the rule editor shows or
#: checks before a save. The server checks all of them again.
_ROUTING_CONSTANTS = (
    "MAX_RULES",
    "MAX_CONDITIONS",
    "MAX_LABEL_BYTES",
    "MAX_PATH_BYTES",
    "MAX_TEST_BYTES",
    "MAX_REASON_BYTES",
    "MAX_REASON_PLACEHOLDERS",
    "ID_PATTERN",
    "AGGREGATES",
)

_MODULE_NAME = "_run_step_catalog_steps"


def load_steps(root: Path = REPO_ROOT) -> ModuleType:
    """The step module of the checkout at ``root``, loaded from its file."""
    path = root / STEPS_MODULE
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the step list from {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered while it runs: a dataclass resolves its annotations through
    # ``sys.modules``.
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(_MODULE_NAME, None)
    return module


def catalog(steps: ModuleType) -> dict[str, Any]:
    """The catalogue: every step in pipeline order, the aliases and resume.

    ``display_steps`` are the steps a run does that it can neither start at nor
    skip, each with the run step it comes right before.
    """
    return {
        "steps": [
            {
                "id": run_step.id,
                "label": run_step.label,
                "required": run_step.required,
                "modes": list(run_step.modes),
                "states": {
                    mode: list(states) for mode, states in run_step.states.items()
                },
            }
            for run_step in steps.RUN_STEPS
        ],
        "display_steps": [
            {
                "id": display.id,
                "label": display.label,
                "before": display.before,
                "review_completed_only": display.review_completed_only,
            }
            for display in steps.DISPLAY_STEPS
        ],
        "aliases": dict(steps.START_ALIASES),
        "resume": steps.RESUME,
    }


def start_values(content: dict[str, Any]) -> list[str]:
    """Every value a surface accepts as a start: the ids, the aliases, resume."""
    return [
        *(entry["id"] for entry in content["steps"]),
        *content["aliases"],
        content["resume"],
    ]


def optional_step_ids(content: dict[str, Any]) -> list[str]:
    """The ids of the steps a run can skip, in pipeline order."""
    return [entry["id"] for entry in content["steps"] if not entry["required"]]


def _module_constants(tree: ast.Module) -> dict[str, Any]:
    """The module-level names assigned a literal, or a name that is one."""
    constants: dict[str, Any] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            constants[target.id] = _literal(node.value, constants)
        except ValueError:
            continue
    return constants


def _literal(node: ast.AST, constants: dict[str, Any]) -> Any:
    """A constant, a tuple of them, or the value of a name already read."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.Tuple):
        return [_literal(element, constants) for element in node.elts]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _literal(node.left, constants) * _literal(node.right, constants)
    raise ValueError(ast.dump(node))


def routing_inputs(root: Path = REPO_ROOT) -> dict[str, Any]:
    """What a routing condition can test, and the limits a rule is held to.

    Read from the source of the routing module: the ``RoutingInput`` fields in
    declaration order, each entry of ``ROUTING_INPUTS``, and the constants of
    ``_ROUTING_CONSTANTS``. A shape this reader does not know is refused rather
    than written wrong.
    """
    path = root / ROUTING_MODULE
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = _module_constants(tree)

    fields: list[str] = []
    defaults: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "RoutingInput":
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and isinstance(
                    statement.target, ast.Name
                ):
                    fields.append(statement.target.id)
                    if statement.value is not None:
                        defaults[statement.target.id] = _literal(
                            statement.value, constants
                        )
    if not fields:
        raise RuntimeError(f"no RoutingInput fields were read from {path}")

    declared = next(
        (
            node.value
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ROUTING_INPUTS"
        ),
        None,
    )
    if not isinstance(declared, ast.Tuple) or not declared.elts:
        raise RuntimeError(f"ROUTING_INPUTS is not a tuple of inputs in {path}")
    inputs = []
    for call in declared.elts:
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "RoutingInput"
        ):
            raise RuntimeError("an entry of ROUTING_INPUTS is not a RoutingInput call")
        values = dict(defaults)
        for name, argument in zip(fields, call.args):
            values[name] = _literal(argument, constants)
        for keyword in call.keywords:
            values[str(keyword.arg)] = _literal(keyword.value, constants)
        missing = [name for name in fields if name not in values]
        if missing:
            raise RuntimeError(f"a routing input has no value for {missing}")
        inputs.append({name: values[name] for name in fields})

    missing = [name for name in _ROUTING_CONSTANTS if name not in constants]
    if missing:
        raise RuntimeError(f"{path} does not assign {missing}")
    return {
        "inputs": inputs,
        "aggregates": constants["AGGREGATES"],
        "id_pattern": constants["ID_PATTERN"],
        "limits": {
            "max_rules": constants["MAX_RULES"],
            "max_conditions": constants["MAX_CONDITIONS"],
            "max_label_bytes": constants["MAX_LABEL_BYTES"],
            "max_path_bytes": constants["MAX_PATH_BYTES"],
            "max_test_bytes": constants["MAX_TEST_BYTES"],
            "max_reason_bytes": constants["MAX_REASON_BYTES"],
            "max_reason_placeholders": constants["MAX_REASON_PLACEHOLDERS"],
        },
    }


def render(content: dict[str, Any]) -> str:
    """The text of a committed copy."""
    return json.dumps(content, indent=2) + "\n"


def generated(root: Path = REPO_ROOT) -> list[tuple[str, tuple[Path, ...]]]:
    """Every generated text with the copies that hold it."""
    return [
        (render(catalog(load_steps(root))), TARGETS),
        (render(routing_inputs(root)), ROUTING_INPUT_TARGETS),
    ]


def stale_targets(root: Path = REPO_ROOT) -> list[tuple[Path, str]]:
    """The committed copies under ``root`` that differ, each with its text."""
    stale = []
    for source, targets in generated(root):
        for target in targets:
            path = root / target
            if not path.exists() or path.read_text() != source:
                stale.append((target, source))
    return stale


def main(argv: list[str] | None = None, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if a committed copy is stale; write nothing",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the step catalogue to stdout; write nothing",
    )
    args = parser.parse_args(argv)

    if args.print_only:
        sys.stdout.write(render(catalog(load_steps(root))))
        return 0

    stale = stale_targets(root)
    if args.check:
        if stale:
            sys.stderr.write(
                "the run step catalogue is stale in:\n"
                + "".join(f"  {target}\n" for target, _ in stale)
                + "regenerate with: python3 scripts/generate_run_step_catalog.py\n"
            )
            return 1
        sys.stdout.write("the run step catalogue is up to date\n")
        return 0

    for target, source in stale:
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    if stale:
        sys.stdout.write("updated:\n" + "".join(f"  {target}\n" for target, _ in stale))
    else:
        sys.stdout.write("already up to date\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
