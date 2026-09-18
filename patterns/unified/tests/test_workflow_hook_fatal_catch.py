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
_UNQUOTED_PLACEHOLDER_RE = re.compile(r"\"\s*:\s*\$\{[A-Za-z0-9_]+\}")

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

    if catch_all_index is not None and _is_fail_state(catchers[catch_all_index]["Next"]):
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
