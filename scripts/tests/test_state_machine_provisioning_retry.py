# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Lambda task retries the code-artifact provisioning errors, on a short budget.

`Invoke` answers `Lambda.CodeArtifactUserPendingException` (HTTP 409) while a
function's code artifact is still being provisioned; the API reference resolves it
as "wait for the function's `State` to become `Active` and try the request again".
`CodeArtifactUserDeletedException` says to wait for Lambda to provision a new one.
A task whose retry policy matches neither fails outright on a condition that clears
itself — observed on a stack idle for weeks, where consecutive documents died at a
different stage each time as the pipeline warmed one function at a time. Once the
errors are retried, the condition cleared within two retries (under two minutes) in
every observed execution.

The budget matters as much as the coverage, which is why the errors get their own
`Retry` entry rather than joining the transient list: an entry that also matches
throttles inherits a ladder sized for throttles (upstream sized one at 8 attempts
from 10 s at 2.5x — hours, not minutes; see 210306f73 for the incident that budget
shape caused). A function that is provisioning becomes `Active` in seconds, so its
entry must be strictly shorter than the transient entry beside it.

Blocks whose policies already match these errors through the `States.TaskFailed`
wildcard keep their existing ladders on purpose: inserting an entry ahead of a
wildcard silently moves errors the wildcard used to catch onto a different budget,
and changing those budgets is a separate decision. These tests therefore accept
wildcard coverage as-is and only police blocks that carry a dedicated entry.

Pure JSON/YAML parsing on purpose, like the other structural gates in this
directory: no imports from Lambda source, which builds AWS clients at module scope.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every state machine in the tree that defines Lambda Task states. Hard-coded on
# purpose, matching the fixed-path idiom of the other gates in this directory: a
# discovery engine is its own source of bugs, and a new state machine added
# without being listed here is exactly the kind of change that should have to
# touch this file.
ASL_JSON_PATHS = {
    "unified-workflow": REPO_ROOT / "patterns/unified/statemachine/workflow.asl.json",
    "multi-doc-discovery": REPO_ROOT
    / "src/lambda/multi_doc_discovery/statemachine.asl.json",
    "backfill-gsi": REPO_ROOT
    / "src/lambda/backfill_gsi_attributes/statemachine.asl.json",
    "finetuning": REPO_ROOT / "src/lambda/finetuning_state_machine/definition.json",
}
# The fifth state machine is defined inline (YAML `Definition:`) in this template.
CONFBENCH_TEMPLATE = REPO_ROOT / "feature-platform/confbench-testset/template.yaml"

PROVISIONING_ERRORS = frozenset(
    {
        "Lambda.CodeArtifactUserPendingException",
        "Lambda.CodeArtifactUserDeletedException",
    }
)

# `CodeArtifactUserFailedException` is deliberately absent: the API reference says
# it needs the function's code package updated, so retrying it only delays a
# failure that will not clear.
NOT_RETRYABLE = "Lambda.CodeArtifactUserFailedException"

WILDCARDS = frozenset({"States.ALL", "States.TaskFailed"})

# ASL defaults for a Retry entry, per the States Language spec. Getting these
# wrong makes the budget arithmetic certify nonsense: with 0/0/1 defaults an
# entry that omits IntervalSeconds computes a zero-second budget regardless of
# MaxAttempts.
ASL_DEFAULT_INTERVAL_SECONDS = 1
ASL_DEFAULT_MAX_ATTEMPTS = 3
ASL_DEFAULT_BACKOFF_RATE = 2.0


# CloudFormation `DefinitionSubstitutions` placeholders. A numeric ASL field needs
# its placeholder unquoted to become an integer after substitution, which makes the
# raw file invalid JSON. Resolve in two passes: a placeholder standing as an entire
# unquoted value becomes a numeric literal; every remaining placeholder lives inside
# a string and collapses to its own name — `"arn:${Partition}:states:::lambda:invoke"`
# must stay recognizable as a Lambda ARN, so the surrounding text is never touched.
_UNQUOTED_PLACEHOLDER_RE = re.compile(r"(:\s*)\$\{[^}]*\}(\s*[,\n\}\]])")
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")


def _load_asl_json(path: Path) -> dict[str, Any]:
    text = path.read_text()
    text = _UNQUOTED_PLACEHOLDER_RE.sub(r"\g<1>1\g<2>", text)
    text = _PLACEHOLDER_RE.sub(r"\g<1>", text)
    return json.loads(text)


class _CfnTagLoader(yaml.SafeLoader):
    """SafeLoader (never the unsafe ``yaml.Loader``) plus CFN short-form tags.

    A dedicated subclass, not ``yaml.SafeLoader`` itself — ``add_constructor``
    mutates the class it is called on, and registering on the shared base leaks
    the constructors into every other SafeLoader user in the process (see the
    sibling ``test_metric_namespace_alignment.py`` for the incident).
    """


def _cfn_tag_loader() -> Any:
    def _make_ctor(tag_name: str):
        def _ctor(loader: Any, node: Any) -> Any:
            if isinstance(node, yaml.ScalarNode):
                return {tag_name: loader.construct_scalar(node)}
            if isinstance(node, yaml.SequenceNode):
                return {tag_name: loader.construct_sequence(node)}
            if isinstance(node, yaml.MappingNode):
                return {tag_name: loader.construct_mapping(node)}
            return {tag_name: None}

        return _ctor

    for tag in (
        "Ref",
        "Sub",
        "GetAtt",
        "Join",
        "If",
        "Not",
        "Equals",
        "And",
        "Or",
        "Select",
        "Split",
        "Base64",
        "Cidr",
        "FindInMap",
        "GetAZs",
        "ImportValue",
        "Condition",
    ):
        _CfnTagLoader.add_constructor(f"!{tag}", _make_ctor(f"!{tag}"))
    return _CfnTagLoader


def _definitions() -> dict[str, dict[str, Any]]:
    """All five state machine definitions, keyed by a stable id."""
    defs = {name: _load_asl_json(path) for name, path in ASL_JSON_PATHS.items()}
    template = yaml.load(  # nosec B506 - SafeLoader subclass, see _CfnTagLoader
        CONFBENCH_TEMPLATE.read_text(), Loader=_cfn_tag_loader()
    )
    inline = {
        logical_id: resource["Properties"]["Definition"]
        for logical_id, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::Serverless::StateMachine"
        and "Definition" in resource.get("Properties", {})
    }
    assert inline, f"{CONFBENCH_TEMPLATE.name}: expected an inline state machine"
    for logical_id, definition in inline.items():
        defs[f"confbench-{logical_id}"] = definition
    return defs


def _task_states(container: dict[str, Any], trail: tuple[str, ...] = ()):
    """Yield (state-name-trail, state) for every Task state, at any nesting.

    Walks only real state containers (``States`` maps, Map iterators, Parallel
    branches), so a ``Retry`` key inside e.g. a Task's ``Parameters`` payload is
    never mistaken for a retry policy.
    """
    for name, state in (container.get("States") or {}).items():
        here = trail + (name,)
        if state.get("Type") == "Task":
            yield here, state
        for key in ("Iterator", "ItemProcessor"):
            if isinstance(state.get(key), dict):
                yield from _task_states(state[key], here)
        for branch in state.get("Branches") or []:
            if isinstance(branch, dict):
                yield from _task_states(branch, here)


def _is_lambda_task(state: dict[str, Any]) -> bool:
    resource = state.get("Resource")
    if not isinstance(resource, str):
        return False
    resource = _PLACEHOLDER_RE.sub(r"\g<1>", resource)
    if ":lambda:" in resource:  # arn:<p>:states:::lambda:invoke[.waitForTaskToken]
        return True
    # A direct function invocation: DefinitionSubstitutions leave a bare
    # placeholder (now collapsed to its name) where a Lambda ARN is injected.
    # Service integrations (dynamodb:updateItem, s3:putObject, ...) keep their
    # full arn:...:states::: form and are excluded here.
    return not resource.startswith("arn:")


def _errors(entry: dict[str, Any]) -> frozenset[str]:
    return frozenset(entry.get("ErrorEquals") or [])


def _worst_case_seconds(entry: dict[str, Any]) -> float:
    attempts = entry.get("MaxAttempts", ASL_DEFAULT_MAX_ATTEMPTS)
    interval = entry.get("IntervalSeconds", ASL_DEFAULT_INTERVAL_SECONDS)
    backoff = entry.get("BackoffRate", ASL_DEFAULT_BACKOFF_RATE)
    max_delay = entry.get("MaxDelaySeconds")
    waits = [interval * backoff**i for i in range(attempts)]
    if max_delay is not None:
        waits = [min(wait, max_delay) for wait in waits]
    return sum(waits)


_DEFINITIONS = _definitions()


@pytest.mark.parametrize("machine", _DEFINITIONS.keys())
def test_every_lambda_task_retries_the_provisioning_errors(machine):
    """A Lambda task that retries transient errors but not these fails while its
    function is still provisioning. Coverage is either a dedicated entry (not
    shadowed by an earlier wildcard) or an existing wildcard entry."""
    uncovered = []
    for trail, state in _task_states(_DEFINITIONS[machine]):
        if not _is_lambda_task(state):
            continue
        retry = state.get("Retry") or []
        dedicated = next(
            (i for i, e in enumerate(retry) if _errors(e) & PROVISIONING_ERRORS), None
        )
        wildcard = next(
            (i for i, e in enumerate(retry) if _errors(e) & WILDCARDS), None
        )
        covered = wildcard is not None or (
            dedicated is not None and (wildcard is None or dedicated < wildcard)
        )
        if not covered:
            uncovered.append(".".join(trail))
    assert not uncovered, (
        f"{machine}: {len(uncovered)} Lambda task(s) retry neither of "
        f"{sorted(PROVISIONING_ERRORS)} nor a wildcard: {uncovered}"
    )


@pytest.mark.parametrize("machine", _DEFINITIONS.keys())
def test_a_dedicated_provisioning_entry_is_effective_and_complete(machine):
    """Where a block carries its own provisioning entry, that entry must actually
    be the one that fires (nothing wildcard ahead of it), must cover both errors,
    and must not smuggle other error names onto its short ladder."""
    for trail, state in _task_states(_DEFINITIONS[machine]):
        if not _is_lambda_task(state):
            continue
        retry = state.get("Retry") or []
        index = next(
            (i for i, e in enumerate(retry) if _errors(e) & PROVISIONING_ERRORS), None
        )
        if index is None:
            continue
        entry_errors = _errors(retry[index])
        where = f"{machine} at {'.'.join(trail)}"
        assert PROVISIONING_ERRORS <= entry_errors, (
            f"{where}: the provisioning entry lists "
            f"{sorted(entry_errors & PROVISIONING_ERRORS)} but not "
            f"{sorted(PROVISIONING_ERRORS - entry_errors)} — both errors clear the "
            "same way and belong on the same entry."
        )
        assert entry_errors <= PROVISIONING_ERRORS, (
            f"{where}: the provisioning entry also matches "
            f"{sorted(entry_errors - PROVISIONING_ERRORS)}. A merged entry silently "
            "moves those errors onto this entry's short ladder; keep it dedicated."
        )
        shadowing = [
            sorted(_errors(e) & WILDCARDS)
            for e in retry[:index]
            if _errors(e) & WILDCARDS
        ]
        assert not shadowing, (
            f"{where}: a wildcard entry {shadowing} precedes the provisioning entry, "
            "so the provisioning entry is dead configuration — Step Functions uses "
            "the first matching entry."
        )


@pytest.mark.parametrize("machine", _DEFINITIONS.keys())
def test_the_provisioning_budget_is_real_and_correctly_sized(machine):
    """The point of a separate entry. `MaxAttempts` must actually retry, and the
    worst-case budget — computed with the real ASL defaults (1 s / 3 attempts /
    2.0 backoff, `MaxDelaySeconds` honored) — must cover the window the condition
    was observed to need, without holding a concurrency slot on a ladder sized
    for throttles. Both bounds come from measurement: the condition cleared
    between 40 s and 106 s of wall time in every observed execution, and the
    ladder it must not resemble runs to hours."""
    for trail, state in _task_states(_DEFINITIONS[machine]):
        if not _is_lambda_task(state):
            continue
        retry = state.get("Retry") or []
        index = next(
            (i for i, e in enumerate(retry) if _errors(e) & PROVISIONING_ERRORS), None
        )
        if index is None:
            continue
        entry = retry[index]
        where = f"{machine} at {'.'.join(trail)}"
        attempts = entry.get("MaxAttempts", ASL_DEFAULT_MAX_ATTEMPTS)
        assert attempts >= 1, (
            f"{where}: MaxAttempts {attempts} never retries — the entry only "
            "suppresses the errors it claims to handle."
        )
        budget = _worst_case_seconds(entry)
        assert budget >= 120, (
            f"{where}: the provisioning schedule spans only {budget:.0f}s. The "
            "condition was observed to clear as late as ~106s after the first "
            "failure, so a shorter budget gives up on documents that would have "
            "recovered."
        )
        assert budget <= 600, (
            f"{where}: the provisioning budget is {budget / 60:.1f} min worst "
            "case. These errors clear when the function becomes Active — in "
            "seconds to about two minutes observed — so a long budget only holds "
            "a concurrency slot for a condition that either resolves quickly or "
            "is not going to resolve."
        )


@pytest.mark.parametrize("machine", _DEFINITIONS.keys())
def test_the_unretryable_provisioning_error_is_not_retried(machine):
    """The other direction, so 'add the CodeArtifact errors' is never read as
    'add all three'. Scoped to Retry — catching it for cleanup is legitimate."""
    for trail, state in _task_states(_DEFINITIONS[machine]):
        for entry in state.get("Retry") or []:
            assert NOT_RETRYABLE not in _errors(entry), (
                f"{machine} at {'.'.join(trail)}: {NOT_RETRYABLE} needs the "
                "function's code package updated; retrying it only delays the "
                "failure."
            )
