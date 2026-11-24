# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which top-level keys a state's input provably has, along every path to it.

A state that reads ``$.document`` when its input has no such key raises
``States.Runtime``: not retriable, not caught by ``States.ALL``, and found only
when an execution reaches that state on that path. ``analyze`` walks a definition
edge by edge from a seed shape, tracks which top-level keys each state's output
is *provably* missing, and reports a state that reads one of them.

It is deliberately one-directional: a finding is reported only when absence is
provable, never when a key is merely unproven, so a Lambda's return shape (which
no static check can know) produces no noise. The one inference that gives it
teeth is that a value read from a ``...document`` path is a document, and a
serialized document has no ``document`` key.

Two gates use it. ``test_state_machine_retry_policies.py`` runs it from the
definition's ``StartAt`` with an open seed (#918). The run-plan gate in
``patterns/unified/tests/test_state_machine_run_plan.py`` runs it from every
state a run plan can start at, with the execution input as a closed seed, so a
start entry that skips the state that builds its inputs fails there.

Pure JSON walking: nothing is imported from Lambda source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Shape:
    """What is provably known about a state input's TOP-LEVEL keys.

    ``present`` are keys that are certainly there; ``absent`` keys that are certainly
    not; ``closed`` means ``present`` is the complete set (the object was built by a
    ``Parameters`` or ``ItemSelector`` block, so its keys are exactly those). Anything
    else is unknown — a Lambda's return value, for instance — and unknown never
    produces a finding.
    """

    present: frozenset[str] = frozenset()
    absent: frozenset[str] = frozenset()
    closed: bool = False

    def lacks(self, key: str) -> bool:
        return key in self.absent or (self.closed and key not in self.present)

    def with_key(self, key: str) -> "Shape":
        return Shape(self.present | {key}, self.absent - {key}, self.closed)


UNKNOWN = Shape()


def first_segment(path: str) -> str | None:
    """``$.HookResults.postOcr.error`` -> ``HookResults``; ``$``/``$$...`` -> None."""
    if not isinstance(path, str) or not path.startswith("$.") or path.startswith("$$"):
        return None
    return path[2:].split(".")[0].split("[")[0] or None


def leaf(path: str) -> str:
    return str(path).split(".")[-1].split("[")[0]


def shape_of_path(path: str) -> Shape:
    """The shape of the value a path selects, where that is knowable.

    The only inference: a ``...document`` path yields a serialized document, and a
    serialized document has no ``document`` key (asserted separately below). That is
    what makes an alternating ``{document: ...}`` / bare-document envelope checkable.
    """
    return (
        Shape(absent=frozenset({"document"})) if leaf(path) == "document" else UNKNOWN
    )


def param_keys(block: dict[str, Any]) -> frozenset[str]:
    return frozenset(k[:-2] if k.endswith(".$") else k for k in block)


def referenced_keys(state: dict[str, Any]) -> set[str]:
    """Top-level input keys this state reads, from every JSONPath it evaluates."""
    keys: set[str] = set()

    def scan(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "Variable" and isinstance(value, str):
                    keys.add(first_segment(value) or "")
                scan(value)
        elif isinstance(node, list):
            for item in node:
                scan(item)
        elif isinstance(node, str) and node.startswith("$."):
            keys.add(first_segment(node) or "")

    # ``Parameters`` / ``ItemSelector`` / ``Choices`` resolve against the EFFECTIVE
    # input, i.e. after ``InputPath`` has narrowed it. No state in this definition
    # sets both, and rather than model the narrowing, a state that does is skipped:
    # a checker that guesses here would report keys that do exist.
    if "InputPath" not in state:
        for field in ("Parameters", "ItemSelector", "Choices"):
            if field in state:
                scan(state[field])
    for field in ("InputPath", "ItemsPath"):
        value = state.get(field)
        if isinstance(value, str):
            keys.add(first_segment(value) or "")
    # A ``Fail`` state's ``CausePath``/``ErrorPath`` are evaluated against its input
    # too, and an unresolvable one there is the worst place for it: the state exists
    # to report a failure, and Step Functions replaces the reported error with
    # ``States.Runtime``, masking whatever actually broke. They hold either a bare
    # JSONPath or an intrinsic-function call, so the paths are matched by pattern
    # rather than by parsing the intrinsic.
    #
    # The ``(?<!\$)`` is load-bearing. Without it the pattern matches the ``$.Xxx``
    # SUBSTRING inside a ``$$.Xxx`` context-object reference — ``$$.Execution.Name``
    # would be read as a reference to an input key ``Execution`` — so a Fail state
    # naming the execution in its cause, which is a natural thing to do, would be
    # reported as reading a key nothing produces. It also makes
    # ``first_segment``'s own ``$$`` guard reachable rather than dead.
    for field in ("CausePath", "ErrorPath"):
        value = state.get(field)
        if isinstance(value, str):
            for match in re.finditer(r"(?<!\$)\$\.[A-Za-z0-9_\[\]]+", value):
                keys.add(first_segment(match.group(0)) or "")
    # ``OutputPath`` is deliberately absent: it filters the state's RESULT (after
    # ResultPath), not its input, so ``"OutputPath": "$.Payload"`` refers to a
    # Lambda's return envelope rather than to anything the input must carry.
    keys.discard("")
    return keys


def result_shape(state: dict[str, Any], incoming: Shape) -> Shape:
    """The shape of the state's RESULT, before ResultPath places it."""
    kind = state.get("Type")
    if kind == "Pass":
        if "Parameters" in state:
            return Shape(param_keys(state["Parameters"]), closed=True)
        if "Result" in state:
            result = state["Result"]
            return (
                Shape(frozenset(result), closed=True)
                if isinstance(result, dict)
                else UNKNOWN
            )
        if "InputPath" in state:
            return shape_of_path(state["InputPath"])
        return incoming
    # A Task's or Map's result is whatever the Lambda / iteration returns: unknown.
    return UNKNOWN


def output_shape(state: dict[str, Any], incoming: Shape) -> Shape:
    """The shape a state passes to its ``Next``."""
    if state.get("Type") in ("Choice", "Wait", "Succeed", "Fail"):
        return incoming
    shape = result_shape(state, incoming)
    # ResultPath is applied to the RAW input, so InputPath does not narrow it.
    result_path = state.get("ResultPath", "$")
    if result_path is None:
        shape = incoming
    elif result_path != "$":
        segment = first_segment(result_path)
        shape = incoming.with_key(segment) if segment else incoming
    output_path = state.get("OutputPath", "$")
    if output_path != "$":
        shape = shape_of_path(output_path)
    return shape


def catch_shape(catcher: dict[str, Any], incoming: Shape) -> Shape:
    """The shape a Catch passes to its target: the raw input plus the error."""
    result_path = catcher.get("ResultPath", "$")
    if result_path is None:
        return incoming
    if result_path == "$":
        return UNKNOWN  # the error object replaces the input entirely
    segment = first_segment(result_path)
    return incoming.with_key(segment) if segment else incoming


def edges(state: dict[str, Any]) -> list[tuple[str, str, Shape | None]]:
    """``(kind, target, override_shape)`` for every outgoing edge."""
    out: list[tuple[str, str, Shape | None]] = []
    for choice in state.get("Choices", []):
        if "Next" in choice:
            out.append(("choice", choice["Next"], None))
    for field in ("Default", "Next"):
        if field in state:
            out.append((field.lower(), state[field], None))
    return out


def analyze(
    states: dict[str, Any], seed: Shape, scope: str, findings: list[str]
) -> None:
    """Propagate shapes to a fixed point, checking every edge as it is taken."""
    start = states["StartAt"] if "StartAt" in states else None
    scope_states: dict[str, Any] = states["States"] if "States" in states else states
    arrivals: dict[str, set[Shape]] = {start: {seed}} if start else {}
    work = [(start, seed)] if start else []
    seen: set[tuple[str, Shape]] = set(work)
    while work:
        name, incoming = work.pop()
        state = scope_states[name]
        # 1. does this state's own input satisfy what it reads?
        for key in referenced_keys(state):
            if incoming.lacks(key):
                findings.append(
                    f"{scope}{name} reads $.{key}, which is provably absent on an "
                    f"incoming path (shape: present={sorted(incoming.present)} "
                    f"absent={sorted(incoming.absent)} closed={incoming.closed})"
                )
        # 2. nested Map / Parallel scopes, seeded from their ItemSelector
        nested_seed = (
            Shape(param_keys(state["ItemSelector"]), closed=True)
            if "ItemSelector" in state
            else incoming
        )
        for nested in ("ItemProcessor", "Iterator"):
            if nested in state:
                analyze(state[nested], nested_seed, f"{scope}{name}/", findings)
        for branch in state.get("Branches", []):
            analyze(branch, nested_seed, f"{scope}{name}/", findings)
        # 3. successors
        successors: list[tuple[str, Shape]] = [
            (target, output_shape(state, incoming)) for _, target, _ in edges(state)
        ]
        for catcher in state.get("Catch", []):
            successors.append((catcher["Next"], catch_shape(catcher, incoming)))
        for target, shape in successors:
            arrivals.setdefault(target, set()).add(shape)
            if (target, shape) not in seen:
                seen.add((target, shape))
                work.append((target, shape))
    # 4. the workflow's output is read as $.document by the tracker
    for name, state in scope_states.items():
        if scope == "" and state.get("End") and state.get("Type") == "Pass":
            for shape in arrivals.get(name, set()):
                if shape.lacks("document"):
                    findings.append(
                        f"{name} ends the workflow with no $.document on one path "
                        f"(present={sorted(shape.present)})"
                    )
