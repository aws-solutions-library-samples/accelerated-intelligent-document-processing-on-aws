# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The workflow definition, and the analysis modules the structural tests share.

``statemachine/workflow.asl.json`` is read through the reachability generator's
own loader and walkers (``scripts/generate_hook_point_reachability.py``), so the
structural tests see the graph the generator derives its table from, and through
the input-shape analysis in ``scripts/tests/state_input_shapes.py``. Both live
outside any package, so they are loaded by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Iterator

PATTERN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PATTERN_ROOT.parents[1]

#: What the execution input holds: the document, the review block of a run that
#: follows a review, and the run plan. The queue processor writes nothing else.
EXECUTION_INPUT_KEYS = frozenset({"document", "review", "runPlan"})

#: The execution-input prefix every run-plan read starts with.
PLAN_PATH_PREFIX = "$$.Execution.Input.runPlan."


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so the dataclass inside resolves its own module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GEN = _load(
    "workflow_definition_generator",
    REPO_ROOT / "scripts" / "generate_hook_point_reachability.py",
)
SHAPES = _load(
    "workflow_definition_shapes",
    REPO_ROOT / "scripts" / "tests" / "state_input_shapes.py",
)


def load_definition() -> dict[str, Any]:
    """The definition, with its CloudFormation placeholders neutralised."""
    return GEN.load_asl()


def all_states(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{qualified name: state} for every state, including those inside Maps.

    A nested state is qualified with its parents, as the generator names it:
    ``ProcessSections.ExtractionShardMap.ShardExtractionStep``.
    """
    return dict(GEN.walk_states(definition["States"]))


def bare(qualified: str) -> str:
    """The state's own name, without the Maps that contain it."""
    return qualified.rsplit(".", 1)[-1]


def is_hook(state: dict[str, Any]) -> bool:
    """A Task that invokes the pipeline hooks dispatcher."""
    function_name = (state.get("Parameters") or {}).get("FunctionName")
    return (
        state.get("Type") == "Task"
        and isinstance(function_name, str)
        and GEN.DISPATCHER_SUBSTITUTION in function_name
    )


def strings(node: Any) -> Iterator[str]:
    """Every string value in a JSON fragment, keys excluded."""
    if isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from strings(item)
    elif isinstance(node, str):
        yield node


def ways_on(state: dict[str, Any], *, catches: bool = True) -> list[str]:
    """Every state this one can hand over to."""
    out = [state[key] for key in ("Next", "Default") if isinstance(state.get(key), str)]
    out += [rule["Next"] for rule in state.get("Choices") or [] if "Next" in rule]
    if catches:
        out += [catch["Next"] for catch in state.get("Catch") or [] if "Next" in catch]
    return out


def reachable_from(states: dict[str, Any], start: str) -> set[str]:
    """Every state of one scope reachable from ``start``, ``start`` included."""
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        if name in seen or name not in states:
            continue
        seen.add(name)
        todo += ways_on(states[name])
    return seen


def first_work_after(states: dict[str, Any], start: str) -> set[str]:
    """The Task and Map states a run meets first after ``start``.

    Follows the ways on through Pass and Choice states and stops at each Task or
    Map. ``start`` itself is not counted. Catch edges are not followed: they
    leave a Task, and the walk stops at one.
    """
    found: set[str] = set()
    seen: set[str] = set()
    todo = ways_on(states[start], catches=False)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if states[name]["Type"] in ("Task", "Map"):
            found.add(name)
            continue
        todo += ways_on(states[name], catches=False)
    return found
