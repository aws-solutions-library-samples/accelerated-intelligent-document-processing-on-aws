# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every pipeline-hook state must fail CLOSED when a hook declares onError: fail.

The `onError: fail` policy is the documented way for an extension to GATE the
pipeline — a PII-redaction or compliance hook that must block a document. The
dispatcher implemented it, but six of the seven hook states caught `States.ALL`
and routed FORWARD, and `States.ALL` matches the dispatcher's fail-policy error
too. ASL takes the FIRST matching catcher, so the fail policy was swallowed and
the document was processed as though the hook had succeeded, with no signal to
anyone (#919).

The invariant below is therefore about ORDER, and it is enumerated from the file
rather than from a list of the six states that were wrong: a seventh hook point
added later is covered the day it appears.

Pure JSON parsing plus one import of the dispatcher's exception module (which has
no imports of its own), so nothing here builds an AWS client.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

PATTERN_ROOT = Path(__file__).resolve().parents[1]
ASL_PATH = PATTERN_ROOT / "statemachine" / "workflow.asl.json"
HOOK_ERRORS_PATH = PATTERN_ROOT / "src" / "pipeline_hooks_function" / "hook_errors.py"

# See test_workflow_evaluation_resilience.py: a numeric ASL field needs its
# CloudFormation placeholder UNQUOTED, which makes the raw file invalid JSON.
#
# The leading `"` anchors the match to a KEY's closing quote, so only a
# placeholder standing where a bare JSON value goes is replaced. Without it the
# pattern also matched INSIDE a quoted string wherever a colon happened to
# precede a placeholder — `"arn:${Partition}:states:::lambda:invoke"` became
# `"arn: 1:states:::lambda:invoke"`, i.e. the parsed document silently
# misrepresented all nine hook/task `Resource` values.
_UNQUOTED_PLACEHOLDER_RE = re.compile(r'"\s*:\s*\$\{[^}]+\}')

# The dispatcher Lambda's ARN substitution. A hook state is identified by the
# function it invokes rather than by its name, so a hook state named differently
# (or added inside another Map) is still found.
_DISPATCHER_SUBSTITUTION = "PipelineHooksDispatcherLambdaArn"


def _load_asl() -> dict:
    return json.loads(_UNQUOTED_PLACEHOLDER_RE.sub('": 1', ASL_PATH.read_text()))


def _fatal_error_name() -> str:
    """The exception class name the dispatcher raises for `onError: fail`.

    Read from the Lambda's own module so that renaming the class without
    renaming it in the ASL fails here instead of in production. Step Functions
    matches a Task error by name: for the optimized `states:::lambda:invoke`
    integration that name is the function-error payload's `errorType`, which the
    Python runtime sets to the exception class's `__name__`.
    """
    spec = importlib.util.spec_from_file_location("hook_errors", HOOK_ERRORS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.HookFatalError.__name__


def _walk(states: dict, scope: str = ""):
    """Yield (qualified_name, state, sibling_states) for every state, including
    those nested in a Map's Iterator/ItemProcessor or a Parallel's Branches.

    `sibling_states` is the States block the state's own `Next` targets resolve
    against — a Fail state inside a Map must live inside that Map.
    """
    for name, state in states.items():
        yield (f"{scope}{name}", state, states)
        for key in ("Iterator", "ItemProcessor"):
            nested = state.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("States"), dict):
                yield from _walk(nested["States"], scope=f"{scope}{name}.")
        for index, branch in enumerate(state.get("Branches") or []):
            if isinstance(branch.get("States"), dict):
                yield from _walk(branch["States"], scope=f"{scope}{name}[{index}].")


def _hook_states() -> dict[str, tuple[dict, dict]]:
    """{qualified name: (state, sibling states)} for every dispatcher Task."""
    found = {}
    for qualified, state, siblings in _walk(_load_asl()["States"]):
        function_name = (state.get("Parameters") or {}).get("FunctionName")
        if (
            state.get("Type") == "Task"
            and isinstance(function_name, str)
            and _DISPATCHER_SUBSTITUTION in function_name
        ):
            found[qualified] = (state, siblings)
    return found


HOOK_STATES = _hook_states()
FATAL_ERROR_NAME = _fatal_error_name()


@pytest.mark.unit
def test_hook_states_are_discovered():
    """A walk that finds nothing would make every assertion below vacuous."""
    assert len(HOOK_STATES) >= 7, (
        f"expected at least the 7 documented hook points, found "
        f"{sorted(HOOK_STATES)} — the walk or the dispatcher substitution name "
        f"({_DISPATCHER_SUBSTITUTION!r}) is wrong, and the ordering assertions "
        f"below would silently pass"
    )


@pytest.mark.unit
@pytest.mark.parametrize("hook_state", sorted(HOOK_STATES))
def test_hook_state_fails_closed_on_the_fail_policy(hook_state):
    """The fail policy must reach a Fail state, not the next pipeline step.

    Two acceptable shapes, both fail-closed:

    * a catcher for the dispatcher's fatal error ordered BEFORE any
      `States.ALL` catcher (every post-step point — their `States.ALL` catcher
      deliberately routes forward so a non-gating hook fault cannot discard an
      otherwise-good document); or
    * a `States.ALL` catcher that already routes to a Fail state
      (`PreprocessingHook`, which must never fall through to processing an
      un-preprocessed original).
    """
    state, siblings = HOOK_STATES[hook_state]
    catchers = state.get("Catch") or []
    assert catchers, (
        f"{hook_state} has no Catch at all: the dispatcher's {FATAL_ERROR_NAME} "
        f"would fail the execution, which is fail-closed, but so would every "
        f"transient dispatcher fault. Add both catchers explicitly."
    )

    def _is_fail_state(next_name):
        return siblings.get(next_name, {}).get("Type") == "Fail"

    catch_all_index = next(
        (i for i, c in enumerate(catchers) if "States.ALL" in c.get("ErrorEquals", [])),
        None,
    )
    fatal_index = next(
        (
            i
            for i, c in enumerate(catchers)
            if FATAL_ERROR_NAME in c.get("ErrorEquals", [])
        ),
        None,
    )

    if catch_all_index is not None and _is_fail_state(
        catchers[catch_all_index]["Next"]
    ):
        return  # States.ALL already terminates the execution.

    assert fatal_index is not None, (
        f"{hook_state} catches States.ALL and routes to "
        f"{catchers[catch_all_index]['Next'] if catch_all_index is not None else '?'}, "
        f"with no catcher for {FATAL_ERROR_NAME}. A hook declaring onError: fail "
        f"would be ignored at this point and the document would be processed as "
        f"though the hook had succeeded (#919)."
    )
    assert catch_all_index is None or fatal_index < catch_all_index, (
        f"{hook_state} lists States.ALL (index {catch_all_index}) BEFORE "
        f"{FATAL_ERROR_NAME} (index {fatal_index}). ASL evaluates catchers in "
        f"array order and takes the first match; States.ALL matches "
        f"{FATAL_ERROR_NAME} too, so in this order the fail policy is dead code."
    )
    target = catchers[fatal_index]["Next"]
    assert _is_fail_state(target), (
        f"{hook_state} routes {FATAL_ERROR_NAME} to {target!r}, which is "
        f"{siblings.get(target, {}).get('Type', 'not a state in this scope')!r}. "
        f"onError: fail must reach a Fail state — routing it anywhere else "
        f"continues the pipeline by another name."
    )


@pytest.mark.unit
def test_no_hook_state_routes_the_fail_policy_forward():
    """Cross-check the same rule from the other side: no hook state may have a
    catcher matching the fatal error that lands on a non-Fail state."""
    offenders = []
    for hook_state, (state, siblings) in HOOK_STATES.items():
        for catcher in state.get("Catch") or []:
            if FATAL_ERROR_NAME not in catcher.get("ErrorEquals", []):
                continue
            target = catcher.get("Next", "")
            if siblings.get(target, {}).get("Type") != "Fail":
                offenders.append(f"{hook_state} -> {target}")
    assert not offenders, (
        f"{FATAL_ERROR_NAME} is caught and routed to a non-Fail state: {offenders}"
    )


def _post_step_hook_states() -> dict[str, tuple[dict, dict]]:
    """The hook states that use the two-catcher, fail-OPEN-on-transient shape.

    Classified by SHAPE, not by name, so a seventh post-step point is covered the
    day it is added: a post-step state carries a distinct `FATAL_ERROR_NAME`
    catcher (to fail closed on the gate) *and* a `States.ALL` catcher (to stay
    open on everything else). `PreprocessingHook` has only `States.ALL`, which
    terminates, so it is correctly excluded.
    """
    selected = {}
    for hook_state, (state, siblings) in HOOK_STATES.items():
        errors = [set(c.get("ErrorEquals") or []) for c in state.get("Catch") or []]
        if any(FATAL_ERROR_NAME in e for e in errors) and any(
            "States.ALL" in e for e in errors
        ):
            selected[hook_state] = (state, siblings)
    return selected


POST_STEP_HOOK_STATES = _post_step_hook_states()


@pytest.mark.unit
def test_post_step_hook_states_are_discovered():
    """Non-vacuity guard for the fail-OPEN half below.

    Floor is the six post-step points that exist today. If a state stops matching
    the two-catcher shape it drops out of the set silently, and the assertion
    below would then pass by testing nothing — which is how the original #919 bug
    survived.
    """
    assert len(POST_STEP_HOOK_STATES) >= 6, (
        f"expected at least the 6 post-step hook points, found "
        f"{sorted(POST_STEP_HOOK_STATES)}. Either a post-step state lost its "
        f"{FATAL_ERROR_NAME} catcher (fail-open on the gate, #919) or lost its "
        f"States.ALL catcher (fail-closed on a transient fault) — both are "
        f"regressions, and both would make the next test vacuous."
    )


@pytest.mark.unit
@pytest.mark.parametrize("hook_state", sorted(POST_STEP_HOOK_STATES))
def test_post_step_hook_state_stays_open_on_a_transient_fault(hook_state):
    """The OTHER half of the invariant: fail closed on the gate, OPEN on a fault.

    `test_hook_state_fails_closed_on_the_fail_policy` proves a hook that declares
    `onError: fail` aborts the document. That is only half the design. At a
    POST-STEP point the `States.ALL` catcher must still route FORWARD, because a
    dispatcher timeout, throttle or cold-start fault is not a gating decision and
    must not discard an otherwise-good document that has already been OCR'd,
    classified and extracted.

    Nothing asserted this. The companion test's early return accepts
    "`States.ALL` already routes to a Fail state" and stops checking — correct for
    `PreprocessingHook`, which must terminate, but it means that flipping a
    post-step `States.ALL` to a Fail state would keep that suite green while
    silently converting every transient dispatcher fault into a discarded
    document.
    """
    state, siblings = POST_STEP_HOOK_STATES[hook_state]
    catchers = state.get("Catch") or []
    catch_all = next(
        c for c in catchers if "States.ALL" in (c.get("ErrorEquals") or [])
    )
    target = catch_all.get("Next", "")
    target_type = siblings.get(target, {}).get("Type", "not a state in this scope")
    assert target_type != "Fail", (
        f"{hook_state} routes its States.ALL catcher to {target!r}, a Fail state. "
        f"At a post-step hook point States.ALL is the TRANSIENT path — a "
        f"dispatcher timeout or throttle — and it must route forward. Failing the "
        f"execution here discards a document that was already processed "
        f"successfully, over a fault the hook author never asked to gate on. "
        f"Only {FATAL_ERROR_NAME} (the declared onError: fail policy) may abort."
    )


# --------------------------------------------------------------------------
# Which hook points each processing mode actually reaches.
#
# `onError: fail` can only abort a document at a hook point the active mode
# EXECUTES. In BDA mode the OCR, classification and extraction steps do not
# exist as separate states, so their hook points are never invoked and a `fail`
# policy registered there is silently inert. That is documented in
# docs/feature-platform.md#not-every-hook-point-exists-in-every-processing-mode
# and docs/feature-platform-developer-guide.md, and tracked as issue #982.
#
# The two tests below pin the table in those docs to the graph, so the docs
# cannot drift from the ASL: if a future change gives the BDA branch its own
# OCR-equivalent state with a postOcr hook, the doc table becomes wrong and
# `test_bda_mode_reaches_no_step_specific_hook_point` fails.
# --------------------------------------------------------------------------

_ROUTER_VARIABLE = "$.document.use_bda"


def _reachable(states: dict, start: str, scope: str = "") -> set[str]:
    """Qualified names of every state reachable from `start` within `states`.

    Follows `Next`, `Default`, `Choices[*].Next` and `Catch[*].Next`. A reached
    Map or Parallel contributes its nested states too, since entering the parent
    executes them.
    """
    seen: set[str] = set()
    queue = [start]
    while queue:
        name = queue.pop()
        if name in seen or name not in states:
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

    reached = set()
    for name in seen:
        reached.add(f"{scope}{name}")
        state = states[name]
        for key in ("Iterator", "ItemProcessor"):
            nested = state.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("States"), dict):
                reached |= _reachable(
                    nested["States"], nested["StartAt"], f"{scope}{name}."
                )
        for index, branch in enumerate(state.get("Branches") or []):
            if isinstance(branch.get("States"), dict):
                reached |= _reachable(
                    branch["States"], branch["StartAt"], f"{scope}{name}[{index}]."
                )
    return reached


def _hook_points_by_mode() -> tuple[str, set[str], set[str], set[str]]:
    """(start_state, always_run, bda_reachable, pipeline_reachable) hook points.

    The mode router is found by the variable it switches on, not by name.
    `always_run` is the hook points executed before the router — `preprocessing`
    is `StartAt`, so it runs in both modes regardless of the branches.
    """
    asl = _load_asl()
    states = asl["States"]
    router = next(
        (
            name
            for name, state in states.items()
            if state.get("Type") == "Choice"
            and any(
                choice.get("Variable") == _ROUTER_VARIABLE
                for choice in state.get("Choices") or []
            )
        ),
        None,
    )
    assert router, (
        f"no Choice state switches on {_ROUTER_VARIABLE!r}; the processing-mode "
        f"router moved or was renamed, and the mode assertions below would be "
        f"meaningless"
    )
    bda_branch = next(
        choice["Next"]
        for choice in states[router]["Choices"]
        if choice.get("Variable") == _ROUTER_VARIABLE
    )
    pipeline_branch = states[router]["Default"]

    def points(names: set[str]) -> set[str]:
        found = set()
        for hook_state, (state, _siblings) in HOOK_STATES.items():
            if hook_state not in names:
                continue
            point = ((state.get("Parameters") or {}).get("Payload") or {}).get(
                "hookPoint"
            )
            assert isinstance(point, str), (
                f"{hook_state} declares no literal hookPoint in its Payload; the "
                f"mode table cannot be checked against the graph"
            )
            found.add(point)
        return found

    # Everything reachable from StartAt but NOT from either branch runs in both
    # modes ahead of the split.
    from_start = _reachable(states, asl["StartAt"])
    from_bda = _reachable(states, bda_branch)
    from_pipeline = _reachable(states, pipeline_branch)
    pre_router = from_start - from_bda - from_pipeline
    return router, points(pre_router), points(from_bda), points(from_pipeline)


ROUTER_STATE, ALWAYS_RUN_POINTS, BDA_POINTS, PIPELINE_POINTS = _hook_points_by_mode()

# The three points that exist only as separate pipeline steps. BDA performs OCR,
# classification and extraction inside one InvokeDataAutomationAsync call, so
# there is no state to hang them off.
_STEP_SPECIFIC_POINTS = {"postOcr", "postClassification", "postExtraction"}


@pytest.mark.unit
def test_every_hook_point_is_classified_by_mode():
    """Non-vacuity guard: the three sets must together cover all seven points."""
    classified = ALWAYS_RUN_POINTS | BDA_POINTS | PIPELINE_POINTS
    declared = {
        ((state.get("Parameters") or {}).get("Payload") or {}).get("hookPoint")
        for state, _siblings in HOOK_STATES.values()
    }
    assert classified == declared, (
        f"the reachability walk from {ROUTER_STATE} classified {sorted(classified)} "
        f"but the graph declares {sorted(declared)}. A hook point reachable from "
        f"neither branch nor before the router is unreachable in BOTH modes, which "
        f"is a worse bug than #982 — or the walk is broken and the mode assertions "
        f"below prove nothing."
    )
    assert ALWAYS_RUN_POINTS == {"preprocessing"}, (
        f"expected `preprocessing` to be the only hook point ahead of "
        f"{ROUTER_STATE}, found {sorted(ALWAYS_RUN_POINTS)}. The docs tell "
        f"extension authors to register at `preprocessing` precisely because it "
        f"is the one point both modes always execute."
    )


@pytest.mark.unit
def test_bda_mode_reaches_no_step_specific_hook_point():
    """BDA mode must not reach postOcr / postClassification / postExtraction.

    This is the reachability claim the docs make. It is asserted in BOTH
    directions so the table cannot drift: the three points are absent from the
    BDA branch and present in the pipeline branch. A `fail` policy registered at
    one of them is therefore inert under BDA — see issue #982.
    """
    leaked = BDA_POINTS & _STEP_SPECIFIC_POINTS
    assert not leaked, (
        f"the BDA branch now reaches {sorted(leaked)}. That is not a regression "
        f"in itself — it may be a fix for #982 — but the mode table in "
        f"docs/feature-platform.md and docs/feature-platform-developer-guide.md "
        f"now claims these points are never invoked under BDA, and must be updated."
    )
    missing = _STEP_SPECIFIC_POINTS - PIPELINE_POINTS
    assert not missing, (
        f"the pipeline branch does NOT reach {sorted(missing)}, so these hook "
        f"points are dead in both modes. Either a hook state was removed or the "
        f"walk is wrong; either way the docs are now wrong too."
    )
    shared = {"postRuleValidation", "postSummarization", "postprocessing"}
    for point in sorted(shared):
        assert point in BDA_POINTS and point in PIPELINE_POINTS, (
            f"{point} is documented as reachable in BOTH processing modes but is "
            f"reached from "
            f"{'pipeline only' if point in PIPELINE_POINTS else 'BDA only'}. "
            f"An `onError: fail` policy there is inert in the other mode."
        )


# --------------------------------------------------------------------------
# A `CausePath` may only read paths its catcher guarantees.
# --------------------------------------------------------------------------


def _causepath_fail_states() -> dict[str, tuple[dict, dict]]:
    """{qualified name: (state, sibling states)} for Fail states using CausePath."""
    return {
        qualified: (state, siblings)
        for qualified, state, siblings in _walk(_load_asl()["States"])
        if state.get("Type") == "Fail" and "CausePath" in state
    }


CAUSEPATH_FAIL_STATES = _causepath_fail_states()

#: Keys that exist only on a caught **error output**, never on a state's own input.
_ERROR_OUTPUT_KEYS = frozenset({"Error", "Cause"})


def _root_key(path: str) -> str | None:
    """`$.HookResults.x` -> `HookResults`; `$` and `$$...` -> None."""
    if not isinstance(path, str) or not path.startswith("$.") or path.startswith("$$"):
        return None
    return path[2:].split(".")[0].split("[")[0] or None


def _keys_read_by(fail_state: dict) -> set[str]:
    """Top-level input keys a Fail state's `CausePath` / `ErrorPath` dereference.

    Both fields hold either a bare JSONPath or an intrinsic-function call, so the
    paths are extracted by pattern rather than by parsing the intrinsic. A bare `$`
    contributes no key: reading the whole input can never be unsatisfiable.
    """
    keys: set[str] = set()
    for field in ("CausePath", "ErrorPath"):
        for match in re.finditer(
            r"\$\.[A-Za-z0-9_\[\]]+", str(fail_state.get(field, ""))
        ):
            key = _root_key(match.group(0))
            if key:
                keys.add(key)
    return keys


@pytest.mark.unit
def test_causepath_fail_states_only_read_paths_their_catchers_guarantee():
    """A `CausePath` must dereference only what the catcher feeding it provides.

    A Fail state's `Error`/`Cause` REPLACE the original error on the
    `ExecutionFailed` event, so a static string discards the only description of
    what actually broke. `CausePath` keeps it — but it is evaluated against the
    Fail state's *input*, and what that input contains is decided by the catcher,
    not by the Fail state. Get the pairing wrong and the reference cannot resolve;
    Step Functions then reports `States.Runtime`, which MASKS the real failure and
    leaves the operator worse off than the static string would have.

    A catcher's `ResultPath` decides which of the two shapes the Fail state sees:

    * absent or `$` — the caught **error output** becomes the whole input, so
      `$.Error` and `$.Cause` resolve and the state's own input keys are gone.
    * `$.Something` — the state's own input survives, with the error output filed
      under `Something`; `$.Cause` does **not** resolve.
    * `null` — the error output is discarded entirely, so nothing describes the
      failure and there is no reason to use `CausePath` at all.

    So the invariant is checked against what each Fail state actually reads rather
    than against one assumed spelling. `PostStepHookFailed` and
    `PostExtractionHookFailed` interpolate `$.Cause` and are fed by catchers with
    no `ResultPath`; `ExtractionShardMapFailed` names the section as well as the
    error (`$.section_id`, `$.ShardMapError`) and is fed by a catcher that files
    the error output beside the input it needs. Both are correct, and the rule
    below admits both while rejecting every mismatch.

    `States.ALL` is refused for all of them regardless of paths: it widens the
    caught error to shapes whose contents are not guaranteed, which is the
    condition that produced the `States.Runtime` masking in the first place.
    """
    assert len(CAUSEPATH_FAIL_STATES) >= 3, (
        f"expected at least the 3 CausePath-bearing Fail states, found "
        f"{sorted(CAUSEPATH_FAIL_STATES)}. If they reverted to a static Cause the "
        f"aborted document again reports nothing about WHAT failed."
    )
    offenders = []
    for _qualified, state, siblings in _walk(_load_asl()["States"]):
        for catcher in state.get("Catch") or []:
            target = catcher.get("Next", "")
            if siblings.get(target, {}).get("Type") != "Fail":
                continue
            if "CausePath" not in siblings[target]:
                continue
            edge = f"{_qualified} -> {target}"
            errors = catcher.get("ErrorEquals") or []
            if "States.ALL" in errors:
                offenders.append(
                    f"{edge}: ErrorEquals={errors} widens the caught error to "
                    f"shapes whose fields are not guaranteed"
                )
                continue
            reads = _keys_read_by(siblings[target])
            result_path = catcher.get("ResultPath", "$")
            payload_root = "$" if result_path == "$" else _root_key(result_path or "")
            if result_path is None:
                offenders.append(
                    f"{edge}: ResultPath is null, so the error output is discarded "
                    f"and nothing the CausePath reads describes the failure"
                )
                continue
            for key in sorted(reads & _ERROR_OUTPUT_KEYS):
                if payload_root != "$":
                    offenders.append(
                        f"{edge}: {target} reads $.{key} but ResultPath="
                        f"{result_path!r} files the error output under "
                        f"$.{payload_root}, so $.{key} does not resolve"
                    )
            for key in sorted(reads - _ERROR_OUTPUT_KEYS):
                if payload_root == "$":
                    offenders.append(
                        f"{edge}: {target} reads $.{key} from the state input, but "
                        f"ResultPath {result_path!r} replaces that input with the "
                        f"error output, so $.{key} does not resolve"
                    )
            if payload_root not in reads and payload_root != "$":
                offenders.append(
                    f"{edge}: the error output is filed under $.{payload_root}, "
                    f"which {target}'s CausePath never reads — the description of "
                    f"the failure is dropped, so the Cause says nothing new"
                )
            elif payload_root == "$" and not (reads & _ERROR_OUTPUT_KEYS):
                offenders.append(
                    f"{edge}: the error output is the whole input, but {target}'s "
                    f"CausePath reads none of {sorted(_ERROR_OUTPUT_KEYS)} — the "
                    f"description of the failure is dropped"
                )
    assert not offenders, (
        "a CausePath-bearing Fail state is fed by a catcher that does not "
        f"guarantee what it reads:\n  " + "\n  ".join(offenders) + "\nEither give "
        "the catcher a ResultPath that matches what the Cause reads, or use a "
        "static Cause at that Fail state."
    )
