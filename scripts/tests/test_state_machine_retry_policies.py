# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A Lambda timeout is retried at most once, and no Catch discards the document.

Two defects in ``patterns/unified/statemachine/workflow.asl.json``, both of which
survived because nothing enumerated the file:

**#917 — deterministic timeouts on a transient ladder.** A function that hits its own
configured timeout reports ``Sandbox.Timedout`` (or ``Lambda.Unknown``, Step Functions'
report for an unhandled fault, of which a timeout and an out-of-memory kill are the two
causes; or ``States.Timeout`` for a state-level timeout). None of those is transient:
the work is deterministic and CPU/IO-bound, so attempt two takes the same wall clock and
ends the same way. Sharing the transient retrier — 8 attempts from 10 s at 2.5x — spent
~5.2 hours of a concurrency slot per document before failing. ``EvaluationStep`` was
given a dedicated single-attempt retrier for exactly this; the other eleven Lambda tasks
kept the shared ladder for another release because the fix was written as a list of
states rather than as a property of the file.

So these tests **enumerate from the definition** rather than naming states. A twelfth
Lambda task added tomorrow with a timeout code on an 8-attempt ladder fails
``test_timeout_retrier_is_single_attempt`` without anyone editing this file — which is
the only version of this gate worth having.

The reverse direction is policed too: ``test_transient_ladder_is_not_weakened`` fails if
the timeout codes are lifted out by collapsing the *throttle* ladder instead, and
``test_timeout_codes_are_not_mixed_into_another_retrier`` keeps the two concerns in
separate retriers so neither budget can be changed by accident. Throttles and service
faults DO succeed on retry and must keep their ladders.

⚠️ ``test_transient_ladder_is_not_weakened`` therefore establishes a **new repo-wide
requirement**, and says so rather than smuggling it in: every Lambda task state in
``workflow.asl.json`` must retry a throttling / service error at least 3 times, so a
state added later with no ``Retry`` block fails. All 24 current states already comply.
The reasoning for keeping it repo-wide instead of narrowing it to the states #917
touched is in that test's own docstring, and the requirement is written up for authors
under "Step Functions Retry Configuration" in ``docs/configuration.md``.

**#918 — a Catch that discards the envelope.** ``RecordEvaluationFailure`` caught with
``ResultPath: null`` and routed to ``PostprocessingHook``, whose ``Parameters`` read
``$.document``. ``ResultPath: null`` means "output is the input unchanged", and the input
on that path is the *bare document dict* — ``$`` IS the document there, which is why the
state passes ``"document.$": "$"``. A document dict has no ``document`` member, so the
path could not resolve and Step Functions raised ``States.Runtime``: not retriable, not
caught by ``States.ALL``, and reached only after every expensive step had already
succeeded and been persisted.

``test_state_input_keys_are_producible`` is the general form of that check. It walks the
definition edge by edge, tracking which top-level keys each state's output is *provably*
missing, and fails when a successor reads one of them. It is deliberately one-directional:
a violation is reported only when absence is provable, never when a key is merely
unproven, so a Lambda's return shape (which no static check can know) produces no noise.
The one inference that gives it teeth is that a value read from a ``...document`` path is
a document, and a serialized document has no ``document`` key —
``test_a_serialized_document_has_no_document_key`` corroborates that against the model
itself.

Scope is the unified document-processing workflow. The other four state-machine
definitions in the tree carry no timeout codes at all, and two of them retry through a
``States.TaskFailed`` wildcard whose budget is a separate decision — see
``test_state_machine_provisioning_retry.py``, which documents that exemption. Pure JSON
parsing, like the other structural gates in this directory: nothing imported from Lambda
source, which builds AWS clients at module scope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ASL_PATH = REPO_ROOT / "patterns/unified/statemachine/workflow.asl.json"

# CloudFormation substitutions. The quoted ones (``"${OCRFunctionArn}"``) are valid JSON
# strings and are left alone — the Resource value is how a Lambda task is recognised.
#
# Anchored on the KEY'S CLOSING QUOTE, not on a bare colon. A bare ``:\s*\$\{…\}`` also
# matches *inside* quoted values: the nine task resources are
# ``"arn:${Partition}:states:::lambda:invoke"``, where a colon sits immediately before
# ``${``, so the naive pattern rewrites all nine to ``"arn: 1:states:::lambda:invoke"``
# and the parsed document stops being a faithful copy of what deploys. Nothing here
# asserts on ``Resource`` text today, so the corruption was invisible — but the next
# assertion about the resource, the partition, or the integration type would have been
# made against mangled text. See ``test_asl_placeholder_substitution.py``, which proves
# behaviourally that no substitution site in the tree damages these ARNs.
_UNQUOTED_PLACEHOLDER_RE = re.compile(r'"\s*:\s*\$\{[^}]+\}')

# Every code that means "the work did not finish in the time available".
TIMEOUT_ERRORS = frozenset({"Sandbox.Timedout", "States.Timeout", "Lambda.Unknown"})
# Wildcards match the timeout codes too, so a wildcard retrier is a timeout retrier.
WILDCARD_ERRORS = frozenset({"States.ALL", "States.TaskFailed"})
# Codes that genuinely clear on retry; their ladders must not be shortened by this fix.
TRANSIENT_ERRORS = frozenset(
    {
        "ThrottlingException",
        "Lambda.TooManyRequestsException",
        "Lambda.ServiceException",
        "ServiceUnavailableException",
    }
)


@pytest.fixture(scope="module")
def definition() -> dict[str, Any]:
    raw = ASL_PATH.read_text(encoding="utf-8")
    return json.loads(_UNQUOTED_PLACEHOLDER_RE.sub('": 1', raw))


def _walk(states: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, dict]]:
    """Every state in the definition, descending into Map and Parallel scopes."""
    for name, state in states.items():
        yield prefix + name, state
        for branch in state.get("Branches", []):
            yield from _walk(branch["States"], f"{prefix}{name}/")
        for nested in ("ItemProcessor", "Iterator"):
            if nested in state:
                yield from _walk(state[nested]["States"], f"{prefix}{name}/")


def _invokes_lambda(state: dict[str, Any]) -> bool:
    if state.get("Type") != "Task":
        return False
    resource = str(state.get("Resource", ""))
    return "lambda:invoke" in resource or bool(
        re.fullmatch(r"\$\{\w*(Function|Lambda)Arn\}", resource)
    )


def _lambda_tasks(definition: dict[str, Any]) -> dict[str, dict]:
    return {n: s for n, s in _walk(definition["States"]) if _invokes_lambda(s)}


def _task_names(definition: dict[str, Any]) -> list[str]:
    return sorted(_lambda_tasks(definition))


# Enumerated at collection time so each state is its own test case: pytest names the
# offending state, and a state added later is picked up with no edit here.
_DEFINITION = json.loads(
    _UNQUOTED_PLACEHOLDER_RE.sub('": 1', ASL_PATH.read_text(encoding="utf-8"))
)
LAMBDA_TASKS = _task_names(_DEFINITION)


def test_enumeration_is_not_vacuous(definition):
    """A parsing bug must not turn the gates below into no-ops.

    The floor is 22 against 24 actually found today (55 states in the definition:
    24 Task, 16 Pass, 7 Choice, 5 Fail, 3 Map, no Parallel). Two states of
    headroom, deliberately: retiring a genuinely obsolete task should not require
    editing this number, and a floor pinned exactly at the current count turns
    every legitimate deletion into a spurious failure. It is still far above what
    the failure this guards would leave — losing a nesting level drops the count
    to 16, losing only ``ProcessSections`` drops it to 17 — so 22 catches every
    way the walk can silently stop descending.

    The named states pin the three depths independently, because a count alone
    cannot: ``ExtractionShardMap/ShardExtractionStep`` is the only task two Map
    levels deep, so a walk that recurses exactly once would still find 23 of 24
    and clear the floor while quietly exempting it.
    """
    tasks = _lambda_tasks(definition)
    assert len(tasks) >= 22, f"only found {len(tasks)} Lambda tasks: {sorted(tasks)}"
    assert "EvaluationStep" in tasks, (
        "EvaluationStep is the reference implementation of the single-attempt "
        "timeout policy; not finding it means the walk is broken"
    )
    assert "ProcessSections/ExtractionPlanStep" in tasks, (
        "tasks nested one Map scope deep are not being enumerated"
    )
    assert "ProcessSections/ExtractionShardMap/ShardExtractionStep" in tasks, (
        "tasks nested TWO Map scopes deep are not being enumerated. This is the "
        "only such state, so nothing else in this file would notice its absence"
    )


@pytest.mark.parametrize("task", LAMBDA_TASKS)
def test_timeout_retrier_is_single_attempt(definition, task):
    """No Lambda task retries a timeout more than once.

    ``MaxAttempts`` counts RETRIES, so 1 means two executions at most: the original
    plus one. That is the deliberate ceiling — a timeout can be caused by a cold
    start or a slow dependency once, but not eight times.
    """
    state = _lambda_tasks(definition)[task]
    for retrier in state.get("Retry", []):
        errors = frozenset(retrier["ErrorEquals"])
        matched = (errors & TIMEOUT_ERRORS) | (errors & WILDCARD_ERRORS)
        if not matched:
            continue
        attempts = retrier.get("MaxAttempts", 3)  # ASL default
        assert attempts <= 1, (
            f"{task} retries {sorted(matched)} {attempts} times at "
            f"{retrier.get('IntervalSeconds')}s x{retrier.get('BackoffRate')}. A "
            "deterministic timeout fails identically on every attempt, so this "
            "burns attempts x the function timeout of a concurrency slot before "
            "failing (#917). Give the timeout codes their own retrier with "
            'MaxAttempts 1, as EvaluationStep does — do NOT shorten the ladder '
            "the throttles share."
        )


#: The error Step Functions raises when a Map exceeds its failure tolerance. A Map
#: retrier on this code re-runs the failed iterations — and with them each failed
#: iteration's OWN retry ladder — so it multiplies a timeout just as a task-level
#: timeout retrier does, one level of indirection away.
MAP_FAILURE_ERRORS = frozenset({"States.ExceedToleratedFailureThreshold"})


def _map_states(definition: dict[str, Any]) -> dict[str, dict]:
    return {n: s for n, s in _walk(definition["States"]) if s.get("Type") == "Map"}


MAP_STATES = sorted(_map_states(_DEFINITION))


def test_map_enumeration_is_not_vacuous(definition):
    """The Map gate below must actually have Maps to check."""
    maps = _map_states(definition)
    assert len(maps) >= 3, f"only found {len(maps)} Map states: {sorted(maps)}"
    assert "ProcessSections/ExtractionShardMap" in maps, (
        "the nested shard Map is not being enumerated, so the only Map in this "
        "definition that retries a failure threshold is exempt from the rule below"
    )


@pytest.mark.parametrize("map_state", MAP_STATES)
def test_map_retrier_does_not_multiply_a_timeout(definition, map_state):
    """#917's rule reaches Map scope, where the cost is a whole ladder, not one try.

    ``test_timeout_retrier_is_single_attempt`` is parametrized over Lambda tasks, so
    a *Map*-level retrier was outside it — and a Map retrier is the more expensive
    of the two. Retrying a Map re-runs its failed iterations, and each of those
    re-runs the iteration's own ``Retry`` ladder from the start. ``MaxAttempts: 1``
    on ``ExtractionShardMap`` therefore does not cost one extra invocation: a shard
    that hit ``Sandbox.Timedout`` costs two more 900 s invocations (its own
    single-attempt timeout retrier included), and a shard failing on the transient
    ladder replays all eight attempts and their 2,550 s of backoff.

    That trade is deliberate and is documented on the retrier itself — the
    alternative is discarding the completed shards of a document. What must not
    happen is the count creeping above one, where a deterministic shard failure
    would multiply whole ladders. Wildcards count too, since they match the timeout
    codes.
    """
    state = _map_states(definition)[map_state]
    for retrier in state.get("Retry", []):
        errors = frozenset(retrier["ErrorEquals"])
        matched = (
            (errors & TIMEOUT_ERRORS)
            | (errors & WILDCARD_ERRORS)
            | (errors & MAP_FAILURE_ERRORS)
        )
        if not matched:
            continue
        attempts = retrier.get("MaxAttempts", 3)  # ASL default
        assert attempts <= 1, (
            f"Map {map_state} retries {sorted(matched)} {attempts} times. Each "
            "attempt re-runs every failed iteration's OWN retry ladder, so this is "
            f"{attempts} x (the iteration's full ladder), not {attempts} extra "
            "invocations. One is the ceiling (#917, #1014)."
        )


@pytest.mark.parametrize("task", LAMBDA_TASKS)
def test_timeout_codes_are_not_mixed_into_another_retrier(definition, task):
    """Timeout codes get a retrier to themselves.

    This is what makes #917's fix expressible: the timeout budget and the throttle
    budget are different numbers, so sharing one retrier means changing one changes
    the other. It also makes the single-attempt gate above impossible to satisfy by
    collapsing a throttle ladder.
    """
    state = _lambda_tasks(definition)[task]
    for retrier in state.get("Retry", []):
        errors = frozenset(retrier["ErrorEquals"])
        if not errors & TIMEOUT_ERRORS:
            continue
        assert errors <= TIMEOUT_ERRORS, (
            f"{task} lists {sorted(errors - TIMEOUT_ERRORS)} in the same retrier as "
            f"{sorted(errors & TIMEOUT_ERRORS)}. Split them: a timeout needs one "
            "attempt, a throttle needs a long ladder."
        )


@pytest.mark.parametrize("task", LAMBDA_TASKS)
def test_transient_ladder_is_not_weakened(definition, task):
    """Throttles and service faults keep a real ladder.

    The failure mode this guards is a well-meant follow-up that reads "retries are
    expensive" and takes the throttle ladders down with the timeout ones. A throttled
    Bedrock or Textract call DOES succeed on retry, and shortening these is how a
    healthy stack starts dropping documents under load.

    **This imposes a repo-wide requirement, stated here because it is new.** The
    first assertion below is not only "do not shorten an existing ladder" — it
    fails a Lambda task state that lists *no* transient retrier at all, so from
    this PR onward **every** Lambda task state in ``workflow.asl.json`` must retry
    at least one of ``ThrottlingException``, ``Lambda.TooManyRequestsException``,
    ``Lambda.ServiceException`` or ``ServiceUnavailableException``, at least 3
    times. All 24 states satisfy it today; a 25th added tomorrow with no ``Retry``
    block fails this test.

    That is deliberate rather than incidental, and it was kept repo-wide rather
    than softened to the states #917 touched. Softening it to a name list is the
    exact defect this file exists to prevent: #917 shipped as a list of states,
    which is why eleven of the twelve then-existing tasks kept the wrong ladder
    for a release. A new Lambda task with no transient retrier is also a real
    defect in its own right — one Bedrock throttle and the document is lost — so
    there is no state for which the requirement is wrong.

    The requirement is documented for authors in ``docs/configuration.md``, under
    "Step Functions Retry Configuration", so it is discoverable from the docs and
    not only from a test failure. If a future state genuinely cannot retry (an
    idempotency hazard, say), the honest change is to add it to a named,
    commented exemption set here — not to delete the assertion.

    Non-Lambda states are out of scope by construction: this test is parametrized
    over ``LAMBDA_TASKS``, which only holds ``Type: Task`` states whose
    ``Resource`` is a Lambda invoke. ``Fail``, ``Choice``, ``Wait``, ``Succeed``,
    ``Pass`` and ``Map`` states can never be reported by it, so every ``Fail``
    state in the definition — and any added later, for instance by a pipeline
    hook's ``onError: fail`` policy or a Map's failure ``Catch`` — is unaffected.
    """
    state = _lambda_tasks(definition)[task]
    ladders = [
        r
        for r in state.get("Retry", [])
        if frozenset(r["ErrorEquals"]) & TRANSIENT_ERRORS
    ]
    assert ladders, (
        f"{task} does not retry any transient service error. Every Lambda task "
        "state in this definition must carry a transient retrier naming at least "
        f"one of {sorted(TRANSIENT_ERRORS)} with MaxAttempts >= 3 — a single "
        "Bedrock or Textract throttle would otherwise lose the document. This is "
        "a repo-wide requirement; see this test's docstring and the "
        '"Step Functions Retry Configuration" section of docs/configuration.md.'
    )
    for retrier in ladders:
        assert retrier.get("MaxAttempts", 3) >= 3, (
            f"{task} retries {sorted(frozenset(retrier['ErrorEquals']) & TRANSIENT_ERRORS)} "
            f"only {retrier.get('MaxAttempts')} times"
        )


def test_no_wildcard_retriers(definition):
    """No Retry matches every error.

    A wildcard silently puts timeouts (and unparseable documents, and bad schemas)
    on whatever ladder it carries, which is how the enumerated gates above would be
    bypassed without naming a timeout code at all.
    """
    offenders = [
        (name, r["ErrorEquals"])
        for name, state in _walk(definition["States"])
        for r in state.get("Retry", [])
        if frozenset(r["ErrorEquals"]) & WILDCARD_ERRORS
    ]
    assert not offenders, f"wildcard Retry blocks: {offenders}"


# ---------------------------------------------------------------------------
# #918: envelope shape across Catch and Next edges
# ---------------------------------------------------------------------------


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


def _first_segment(path: str) -> str | None:
    """``$.HookResults.postOcr.error`` -> ``HookResults``; ``$``/``$$...`` -> None."""
    if not isinstance(path, str) or not path.startswith("$.") or path.startswith("$$"):
        return None
    return path[2:].split(".")[0].split("[")[0] or None


def _leaf(path: str) -> str:
    return str(path).split(".")[-1].split("[")[0]


def _shape_of_path(path: str) -> Shape:
    """The shape of the value a path selects, where that is knowable.

    The only inference: a ``...document`` path yields a serialized document, and a
    serialized document has no ``document`` key (asserted separately below). That is
    what makes an alternating ``{document: ...}`` / bare-document envelope checkable.
    """
    return Shape(absent=frozenset({"document"})) if _leaf(path) == "document" else UNKNOWN


def _param_keys(block: dict[str, Any]) -> frozenset[str]:
    return frozenset(k[:-2] if k.endswith(".$") else k for k in block)


def _referenced_keys(state: dict[str, Any]) -> set[str]:
    """Top-level input keys this state reads, from every JSONPath it evaluates."""
    keys: set[str] = set()

    def scan(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "Variable" and isinstance(value, str):
                    keys.add(_first_segment(value) or "")
                scan(value)
        elif isinstance(node, list):
            for item in node:
                scan(item)
        elif isinstance(node, str) and node.startswith("$."):
            keys.add(_first_segment(node) or "")

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
            keys.add(_first_segment(value) or "")
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
    # ``_first_segment``'s own ``$$`` guard reachable rather than dead.
    for field in ("CausePath", "ErrorPath"):
        value = state.get(field)
        if isinstance(value, str):
            for match in re.finditer(r"(?<!\$)\$\.[A-Za-z0-9_\[\]]+", value):
                keys.add(_first_segment(match.group(0)) or "")
    # ``OutputPath`` is deliberately absent: it filters the state's RESULT (after
    # ResultPath), not its input, so ``"OutputPath": "$.Payload"`` refers to a
    # Lambda's return envelope rather than to anything the input must carry.
    keys.discard("")
    return keys


def _result_shape(state: dict[str, Any], incoming: Shape) -> Shape:
    """The shape of the state's RESULT, before ResultPath places it."""
    kind = state.get("Type")
    if kind == "Pass":
        if "Parameters" in state:
            return Shape(_param_keys(state["Parameters"]), closed=True)
        if "Result" in state:
            result = state["Result"]
            return Shape(frozenset(result), closed=True) if isinstance(result, dict) else UNKNOWN
        if "InputPath" in state:
            return _shape_of_path(state["InputPath"])
        return incoming
    # A Task's or Map's result is whatever the Lambda / iteration returns: unknown.
    return UNKNOWN


def _output_shape(state: dict[str, Any], incoming: Shape) -> Shape:
    """The shape a state passes to its ``Next``."""
    if state.get("Type") in ("Choice", "Wait", "Succeed", "Fail"):
        return incoming
    shape = _result_shape(state, incoming)
    # ResultPath is applied to the RAW input, so InputPath does not narrow it.
    result_path = state.get("ResultPath", "$")
    if result_path is None:
        shape = incoming
    elif result_path != "$":
        segment = _first_segment(result_path)
        shape = incoming.with_key(segment) if segment else incoming
    output_path = state.get("OutputPath", "$")
    if output_path != "$":
        shape = _shape_of_path(output_path)
    return shape


def _catch_shape(catcher: dict[str, Any], incoming: Shape) -> Shape:
    """The shape a Catch passes to its target: the raw input plus the error."""
    result_path = catcher.get("ResultPath", "$")
    if result_path is None:
        return incoming
    if result_path == "$":
        return UNKNOWN  # the error object replaces the input entirely
    segment = _first_segment(result_path)
    return incoming.with_key(segment) if segment else incoming


def _edges(state: dict[str, Any]) -> list[tuple[str, str, Shape | None]]:
    """``(kind, target, override_shape)`` for every outgoing edge."""
    out: list[tuple[str, str, Shape | None]] = []
    for choice in state.get("Choices", []):
        if "Next" in choice:
            out.append(("choice", choice["Next"], None))
    for field in ("Default", "Next"):
        if field in state:
            out.append((field.lower(), state[field], None))
    return out


def _analyze(states: dict[str, Any], seed: Shape, scope: str, findings: list[str]) -> None:
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
        for key in _referenced_keys(state):
            if incoming.lacks(key):
                findings.append(
                    f"{scope}{name} reads $.{key}, which is provably absent on an "
                    f"incoming path (shape: present={sorted(incoming.present)} "
                    f"absent={sorted(incoming.absent)} closed={incoming.closed})"
                )
        # 2. nested Map / Parallel scopes, seeded from their ItemSelector
        nested_seed = (
            Shape(_param_keys(state["ItemSelector"]), closed=True)
            if "ItemSelector" in state
            else incoming
        )
        for nested in ("ItemProcessor", "Iterator"):
            if nested in state:
                _analyze(state[nested], nested_seed, f"{scope}{name}/", findings)
        for branch in state.get("Branches", []):
            _analyze(branch, nested_seed, f"{scope}{name}/", findings)
        # 3. successors
        successors: list[tuple[str, Shape]] = [
            (target, _output_shape(state, incoming)) for _, target, _ in _edges(state)
        ]
        for catcher in state.get("Catch", []):
            successors.append((catcher["Next"], _catch_shape(catcher, incoming)))
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


def test_state_input_keys_are_producible(definition):
    """Every state's referenced top-level keys survive the path that reaches it.

    This is the general form of #918: it does not know the name
    ``RecordEvaluationFailure``, only that a Catch with ``ResultPath: null`` hands its
    target the input it received, and that a bare document has no ``document`` key.

    ⚠️ It also covers a ``Fail`` state's ``CausePath``/``ErrorPath``, and that half
    of the coverage is **shared with another suite**: ``patterns/unified/tests/
    test_workflow_hook_fatal_catch.py::
    test_causepath_fail_states_only_read_paths_their_catchers_guarantee`` checks that
    the catcher files the error output where the cause looks for it, while the check
    here is what catches a cause reading an input key nothing on the path produces —
    a ``$.sectionId`` typo for ``$.section_id``, say. Neither is redundant and
    neither subsumes the other; dropping either leaves the other silently weaker,
    and both failure modes surface as a ``States.Runtime`` that masks the real error.
    """
    findings: list[str] = []
    # The execution starts as {"document": ...}; more keys may be present, so open.
    _analyze(definition, Shape(frozenset({"document"})), "", findings)
    assert not findings, "unsatisfiable input paths:\n  " + "\n  ".join(findings)


def test_record_evaluation_failure_catch_preserves_the_document(definition):
    """The specific #918 regression, named, so the fix cannot be quietly reverted."""
    state = definition["States"]["RecordEvaluationFailure"]
    catchers = state["Catch"]
    assert len(catchers) == 1
    catcher = catchers[0]
    result_path = catcher.get("ResultPath", "$")
    assert result_path is not None, (
        "ResultPath null makes this state's output its own input, which on this path "
        "is the bare document dict with no $.document member (#918)"
    )
    assert result_path not in ("$", "$.document"), (
        f"ResultPath {result_path!r} clobbers the document instead of parking the error"
    )
    target = definition["States"][catcher["Next"]]
    assert _param_keys(target.get("Parameters", {})) == {"document"}, (
        f"{catcher['Next']} must rebuild the {{document: ...}} envelope that "
        "PostprocessingHook and the workflow tracker read"
    )
    assert target.get("ResultPath") == "$"
    # Its own timeout is deterministic too, and reaching the Catch is the point.
    timeout_retriers = [
        r for r in state["Retry"] if frozenset(r["ErrorEquals"]) & TIMEOUT_ERRORS
    ]
    assert timeout_retriers, "RecordEvaluationFailure does not name its timeout codes"
    assert all(r["MaxAttempts"] <= 1 for r in timeout_retriers)


def test_a_serialized_document_has_no_document_key():
    """Corroborates the one inference ``_shape_of_path`` makes.

    Skipped rather than failed when ``idp_common`` is not importable: the shape check
    above is a JSON-only gate and must not depend on the library being installed.
    """
    models = pytest.importorskip("idp_common.models")
    document = models.Document(id="doc", input_key="doc.pdf", input_bucket="b")
    assert "document" not in document.to_dict()
    # ``compress()`` needs S3, so check its wrapper literal instead — that shape is
    # the other thing an ASL ``...document`` path can hold.
    import inspect

    source = inspect.getsource(models.Document.compress)
    assert '"document":' not in source
