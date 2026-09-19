#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Derive which pipeline hook points each processing mode reaches, from the ASL.

`patterns/unified/statemachine/workflow.asl.json` is the only authority on which
hook points a document actually passes through: the `RouteByProcessingMode`
Choice splits the graph into a BDA branch and a Pipeline branch, and three of the
seven hook points (`postOcr`, `postClassification`, `postExtraction`) exist only
on the Pipeline side. A hook registered at one of those while the configuration
sets `use_bda: true` can never fire — including its `onError: fail` gate (#982).

Three components need that table, and none of them can read the ASL at runtime:

* `patterns/unified/src/pipeline_hooks_function/` — the dispatcher, packaged
  boto3-only, with the ASL outside its CodeUri.
* `feature-platform/main-stack-extensions/lambdas/register_feature_hooks/` —
  likewise boto3-only, and in a different nested stack entirely.
* `lib/idp_common_pkg/idp_common/config/` — the config-write validation path,
  which runs inside the API resolvers and the CLI.

So the table is GENERATED into each of them as an identical Python module and
committed. A hardcoded list of three names would go stale silently the moment a
hook point is added or moved — the same class of defect as the bug it guards
against — which is why `patterns/unified/tests/test_hook_point_reachability.py`
re-derives the table from the ASL on every test run and fails if any committed
copy differs. Run this script when that test fails:

    python3 scripts/generate_hook_point_reachability.py           # rewrite copies
    python3 scripts/generate_hook_point_reachability.py --check   # CI-style check
    python3 scripts/generate_hook_point_reachability.py --print   # stdout only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
ASL_PATH = REPO_ROOT / "patterns" / "unified" / "statemachine" / "workflow.asl.json"

# Every committed copy of the generated module. All are byte-identical.
TARGETS = (
    Path("patterns/unified/src/pipeline_hooks_function/hook_point_reachability.py"),
    Path(
        "feature-platform/main-stack-extensions/lambdas/register_feature_hooks/"
        "hook_point_reachability.py"
    ),
    Path("lib/idp_common_pkg/idp_common/config/hook_point_reachability.py"),
)

# The ASL carries CloudFormation substitutions, and a NUMERIC field
# (`TimeoutSeconds`) needs its placeholder UNQUOTED, which makes the raw file
# invalid JSON. The leading `"` anchors the match to a KEY's closing quote so
# only a placeholder standing where a bare JSON value goes is replaced — without
# it the pattern also matches inside a string, turning
# `"arn:${Partition}:states:::lambda:invoke"` into `"arn: 1:states:..."`.
_UNQUOTED_PLACEHOLDER_RE = re.compile(r'"\s*:\s*\$\{[^}]+\}')

# The dispatcher Lambda's ARN substitution. A hook state is identified by the
# FUNCTION it invokes, not by its name, so a renamed or newly nested hook state
# is still found.
DISPATCHER_SUBSTITUTION = "PipelineHooksDispatcherLambdaArn"

# The Choice variable that selects the processing mode. Finding the router by the
# variable it switches on rather than by state name means a rename cannot
# silently turn this into "no router, no table".
ROUTER_VARIABLE = "$.document.use_bda"


def load_asl(path: Path = ASL_PATH) -> dict[str, Any]:
    """Parse the ASL with its CloudFormation placeholders neutralised."""
    return json.loads(_UNQUOTED_PLACEHOLDER_RE.sub('": 1', path.read_text()))


def walk_states(states: dict[str, Any], scope: str = "") -> Iterator[tuple[str, dict]]:
    """Yield (qualified name, state) for every state, including nested ones.

    A state inside a Map's `ItemProcessor`/`Iterator` or a Parallel branch is
    qualified with its parent's name — `ProcessSections.PostExtractionHook` —
    because that is where `PostExtractionHook` lives.
    """
    for name, state in states.items():
        yield f"{scope}{name}", state
        for key in ("Iterator", "ItemProcessor"):
            nested = state.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("States"), dict):
                yield from walk_states(nested["States"], f"{scope}{name}.")
        for index, branch in enumerate(state.get("Branches") or []):
            if isinstance(branch.get("States"), dict):
                yield from walk_states(branch["States"], f"{scope}{name}[{index}].")


def hook_states(asl: dict[str, Any]) -> dict[str, str]:
    """{qualified state name: hookPoint} for every dispatcher Task in the graph."""
    found: dict[str, str] = {}
    for qualified, state in walk_states(asl["States"]):
        parameters = state.get("Parameters") or {}
        function_name = parameters.get("FunctionName")
        if state.get("Type") != "Task" or not isinstance(function_name, str):
            continue
        if DISPATCHER_SUBSTITUTION not in function_name:
            continue
        point = (parameters.get("Payload") or {}).get("hookPoint")
        if not isinstance(point, str):
            raise SystemExit(
                f"hook state {qualified} declares no literal hookPoint in its "
                f"Payload, so its hook point cannot be derived from the graph"
            )
        found[qualified] = point
    return found


def reachable(
    states: dict[str, Any],
    start: str,
    scope: str = "",
    blocked: frozenset[str] = frozenset(),
) -> set[str]:
    """Qualified names of every state reachable from `start`.

    Follows `Next`, `Default`, `Choices[*].Next` and `Catch[*].Next`. Entering a
    Map or Parallel executes its nested states, so a reached parent contributes
    its children too. `blocked` names states the walk must not enter — used to
    stop a branch walk from re-entering the mode router and picking up the OTHER
    branch, which would make the whole table vacuously "everything reaches
    everything".
    """
    seen: set[str] = set()
    queue = [start]
    while queue:
        name = queue.pop()
        if name in seen or name in blocked or name not in states:
            continue
        seen.add(name)
        state = states[name]
        for key in ("Next", "Default"):
            if isinstance(state.get(key), str):
                queue.append(state[key])
        for choice in state.get("Choices") or []:
            if isinstance(choice.get("Next"), str):
                queue.append(choice["Next"])
        for catcher in state.get("Catch") or []:
            if isinstance(catcher.get("Next"), str):
                queue.append(catcher["Next"])

    reached: set[str] = set()
    for name in seen:
        reached.add(f"{scope}{name}")
        state = states[name]
        for key in ("Iterator", "ItemProcessor"):
            nested = state.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("States"), dict):
                reached |= reachable(
                    nested["States"], nested["StartAt"], f"{scope}{name}."
                )
        for index, branch in enumerate(state.get("Branches") or []):
            if isinstance(branch.get("States"), dict):
                reached |= reachable(
                    branch["States"], branch["StartAt"], f"{scope}{name}[{index}]."
                )
    return reached


def find_router(states: dict[str, Any]) -> str:
    """The Choice state that selects the processing mode."""
    for name, state in states.items():
        if state.get("Type") != "Choice":
            continue
        if any(
            choice.get("Variable") == ROUTER_VARIABLE
            for choice in state.get("Choices") or []
        ):
            return name
    raise SystemExit(
        f"no Choice state switches on {ROUTER_VARIABLE!r}; the processing-mode "
        f"router moved or was renamed and the reachability table cannot be derived"
    )


def derive(asl: dict[str, Any]) -> dict[str, Any]:
    """The reachability table: hook states and hook points, per processing mode.

    A hook point ahead of the router (today `preprocessing`, which is `StartAt`)
    runs in BOTH modes, so it is folded into both mode entries rather than
    reported separately — callers ask "does this mode reach this point?" and that
    is the answer for either mode.
    """
    states = asl["States"]
    router = find_router(states)
    hooks = hook_states(asl)
    bda_entry = next(
        choice["Next"]
        for choice in states[router]["Choices"]
        if choice.get("Variable") == ROUTER_VARIABLE
    )
    pipeline_entry = states[router]["Default"]

    block = frozenset({router})
    from_start = reachable(states, asl["StartAt"])
    from_bda = reachable(states, bda_entry, blocked=block)
    from_pipeline = reachable(states, pipeline_entry, blocked=block)
    pre_router = from_start - from_bda - from_pipeline

    def state_names(scope: set[str]) -> list[str]:
        return sorted(name for name in hooks if name in scope)

    def points(scope: set[str]) -> set[str]:
        return {point for name, point in hooks.items() if name in scope}

    bda_states = state_names(pre_router | from_bda)
    pipeline_states = state_names(pre_router | from_pipeline)
    return {
        "router": router,
        "entries": {"bda": bda_entry, "pipeline": pipeline_entry},
        "always_states": state_names(pre_router),
        "states_by_mode": {"bda": bda_states, "pipeline": pipeline_states},
        "points_by_mode": {
            "bda": sorted(points(pre_router | from_bda)),
            "pipeline": sorted(points(pre_router | from_pipeline)),
        },
        "all_points": sorted(set(hooks.values())),
    }


def _frozenset_literal(values: list[str], indent: str) -> str:
    inner = "".join(f'{indent}        "{v}",\n' for v in values)
    return (
        f"frozenset(\n{indent}    {{\n{inner}{indent}    }}\n{indent})"
        if values
        else "frozenset()"
    )


def _tuple_literal(values: list[str], indent: str) -> str:
    inner = "".join(f'{indent}    "{v}",\n' for v in values)
    return f"(\n{inner}{indent})" if values else "()"


def render(table: dict[str, Any]) -> str:
    """The generated module source. Identical in every target directory."""
    router = table["router"]
    bda_entry = table["entries"]["bda"]
    pipeline_entry = table["entries"]["pipeline"]
    rows = "\n".join(
        f"#   {point:<20} bda={'yes' if point in table['points_by_mode']['bda'] else 'NO '}"
        f"  pipeline={'yes' if point in table['points_by_mode']['pipeline'] else 'NO '}"
        for point in table["all_points"]
    )
    return f'''# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which pipeline hook points each processing mode actually reaches.

GENERATED FILE — do not edit. Regenerate with:

    python3 scripts/generate_hook_point_reachability.py

Derived by walking patterns/unified/statemachine/workflow.asl.json from
`{table["always_states"][0] if table["always_states"] else "StartAt"}` down each side of the `{router}` Choice
(`{ROUTER_VARIABLE} BooleanEquals true` -> `{bda_entry}`; Default ->
`{pipeline_entry}`), blocking re-entry to the Choice and descending into any Map
or Parallel. A hook point ahead of the Choice runs in both modes and so appears
under both.

{rows}

BDA performs OCR, classification and extraction inside one Bedrock Data
Automation invocation, so the BDA branch has no separate step to hook after and
the three step-specific points do not exist there. A hook registered at one of
them under `use_bda: true` never runs, and neither does its `onError: fail`
gate — the dispatcher is never invoked, so nothing raises and nothing appears in
the execution history (#982). Callers use :func:`unreachable_hook_points` to say
so at registration time and at runtime instead of leaving it silent.
"""

from __future__ import annotations

# Hook `Task` states each branch reaches, qualified by their enclosing Map where
# there is one. Informational: the point sets below are what callers act on, but
# these names are what a reader checks against the state machine in the console.
HOOK_STATES_BY_MODE = {{
    "bda": {_tuple_literal(table["states_by_mode"]["bda"], "    ")},
    "pipeline": {_tuple_literal(table["states_by_mode"]["pipeline"], "    ")},
}}

# Hook points invoked in each mode. Mode names are this module's own labels for
# the two sides of the `{router}` Choice.
HOOK_POINTS_BY_MODE = {{
    "bda": {_frozenset_literal(table["points_by_mode"]["bda"], "    ")},
    "pipeline": {_frozenset_literal(table["points_by_mode"]["pipeline"], "    ")},
}}

# Every hook point the state machine invokes in at least one mode.
ALL_HOOK_POINTS = {_frozenset_literal(table["all_points"], "")}


def processing_mode(use_bda: bool) -> str:
    """This module's label for the branch `{ROUTER_VARIABLE}` selects."""
    return "bda" if use_bda else "pipeline"


def reachable_hook_points(use_bda: bool) -> frozenset[str]:
    """Hook points the chosen branch invokes."""
    return HOOK_POINTS_BY_MODE[processing_mode(use_bda)]


def unreachable_hook_points(use_bda: bool) -> frozenset[str]:
    """Hook points that exist in the OTHER branch but not in this one.

    A hook registered at one of these cannot run in this mode. Empty for a mode
    that reaches every point.
    """
    return ALL_HOOK_POINTS - reachable_hook_points(use_bda)
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any committed copy is stale; write nothing",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the generated module to stdout; write nothing",
    )
    args = parser.parse_args(argv)

    source = render(derive(load_asl()))
    if args.print_only:
        sys.stdout.write(source)
        return 0

    stale: list[str] = []
    for target in TARGETS:
        path = REPO_ROOT / target
        current = path.read_text() if path.exists() else None
        if current == source:
            continue
        stale.append(str(target))
        if not args.check:
            path.write_text(source)

    if args.check:
        if stale:
            sys.stderr.write(
                "hook-point reachability table is stale in:\n"
                + "".join(f"  {t}\n" for t in stale)
                + "regenerate with: python3 scripts/generate_hook_point_reachability.py\n"
            )
            return 1
        sys.stdout.write("hook-point reachability table is up to date\n")
        return 0

    if stale:
        sys.stdout.write("updated:\n" + "".join(f"  {t}\n" for t in stale))
    else:
        sys.stdout.write("already up to date\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
