# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""State names in the workflow definition follow one scheme.

A state's name says what kind of state it is: a step that does work ends in
``Step``, a hook starts with ``Pre`` or ``Post`` and ends in ``Hook``, a choice
starts with ``Check``, and so on. The step viewer, the execution history and
the docs all show these names, and a reader tells a choice from a step by the
name alone.

Every state is checked, including the ones inside a Map and inside the
distributed Map's item processor, and there is no list of names that are let
off. Each state is its own case, so a failure names the state and the rule it
breaks.
"""

from __future__ import annotations

import copy
import re

import pytest
from _workflow_definition import all_states, bare, is_hook, load_definition

DEFINITION = load_definition()
STATES = all_states(DEFINITION)

HOOK_NAME = re.compile(r"^(Pre|Post)[A-Za-z]+Hook$")
TASK_NAME = re.compile(r"^(Pipeline_|BDA_)?[A-Z][A-Za-z]*Step$")
CHOICE_NAME = re.compile(r"^Check[A-Z][A-Za-z]*$")
PASS_PREFIXES = ("Apply", "Mark", "Set", "Normalize", "Select", "Skip", "StartAt")
MODE_PREFIXES = ("Pipeline_", "BDA_")
#: Words that named the review decision and the assembly step in earlier
#: definitions. A state that carries one describes something the definition no
#: longer does.
RETIRED_WORDS = ("HITL", "ProcessResults")
#: The only spellings ``RuleValidation`` may be part of.
RULE_VALIDATION_FORMS = ("PolicyRuleValidation", "PostRuleValidationHook")


def name_problem(name: str, state: dict) -> str | None:
    """Why ``name`` does not fit the rule for its state type, or ``None``."""
    kind = state.get("Type")
    if kind == "Task" and is_hook(state):
        if not HOOK_NAME.match(name):
            return "a hook state is named Pre…Hook or Post…Hook"
    elif kind == "Task":
        if not TASK_NAME.match(name):
            return "a step is named …Step, with an optional Pipeline_ or BDA_ prefix"
    elif kind == "Choice":
        if not CHOICE_NAME.match(name):
            return "a choice is named Check…"
    elif kind == "Map":
        if not name[:1].isupper():
            return "a Map name starts with an upper-case letter"
        if name.endswith(("Step", "Hook")):
            return "a Map is not named …Step or …Hook (those are Tasks)"
        if name.startswith("Check"):
            return "a Map is not named Check… (that is a Choice)"
    elif kind == "Pass":
        if not (name.startswith(PASS_PREFIXES) or name.endswith("Complete")):
            return (
                "a Pass state starts with Apply, Mark, Set, Normalize, Select, Skip "
                "or StartAt, or ends in Complete"
            )
    elif kind == "Fail":
        if not name.endswith("Failed"):
            return "a Fail state ends in Failed"
    else:
        return f"no naming rule covers the state type {kind!r}; add one here"
    return None


def word_problem(name: str) -> str | None:
    """Why ``name`` uses a word it must not, or ``None``."""
    for word in RETIRED_WORDS:
        if word in name:
            return f"no state name contains {word}"
    remainder = name
    for form in RULE_VALIDATION_FORMS:
        remainder = remainder.replace(form, "")
    if "RuleValidation" in remainder:
        return (
            "RuleValidation appears only as part of PolicyRuleValidation or "
            "PostRuleValidationHook"
        )
    return None


def mode_pair_problems(names: set[str]) -> list[str]:
    """A ``Pipeline_X`` state without its ``BDA_X`` state, and the reverse."""
    problems = []
    for name in sorted(names):
        for prefix, other in (MODE_PREFIXES, MODE_PREFIXES[::-1]):
            if name.startswith(prefix) and other + name[len(prefix) :] not in names:
                problems.append(
                    f"{name} has no {other}{name[len(prefix) :]} state: a name carries "
                    f"a mode prefix only where both modes have the step"
                )
    return problems


def test_the_enumeration_reaches_every_depth():
    """A walk that stops descending would let nested states off."""
    kinds = {state["Type"] for state in STATES.values()}
    assert kinds == {"Task", "Choice", "Map", "Pass", "Fail"}, kinds
    assert "ProcessSections.AssessmentStep" in STATES
    # Two Maps deep, inside the distributed Map's item processor.
    assert "ProcessSections.ExtractionShardMap.ShardExtractionStep" in STATES


def test_no_two_states_share_a_name():
    """A name is unique in the whole definition, not only in its scope.

    Amazon States Language requires it, and the execution history and the step
    viewer show the bare name, so two states with one name in different Maps
    could not be told apart there.
    """
    names = [bare(qualified) for qualified in STATES]
    assert sorted(names) == sorted(set(names))


@pytest.mark.parametrize("qualified", sorted(STATES))
def test_the_name_fits_its_state_type(qualified):
    problem = name_problem(bare(qualified), STATES[qualified])
    assert problem is None, f"{qualified} ({STATES[qualified]['Type']}): {problem}"


@pytest.mark.parametrize("qualified", sorted(STATES))
def test_the_name_uses_no_retired_word(qualified):
    problem = word_problem(bare(qualified))
    assert problem is None, f"{qualified}: {problem}"


def test_a_mode_prefix_comes_in_pairs():
    names = {bare(qualified) for qualified in STATES}
    # Not vacuous: the definition has such a pair.
    assert any(name.startswith(MODE_PREFIXES) for name in names)
    assert mode_pair_problems(names) == []


# ---------------------------------------------------------------------------
# The rules fire: each one on a name that breaks it
# ---------------------------------------------------------------------------

_TASK = {"Type": "Task", "Resource": "${OCRFunctionArn}"}
_HOOK = {
    "Type": "Task",
    "Resource": "arn:${Partition}:states:::lambda:invoke",
    "Parameters": {"FunctionName": "${PipelineHooksDispatcherLambdaArn}"},
}


@pytest.mark.parametrize(
    "name, state",
    [
        ("RoutingDecisionChoice", {"Type": "Choice"}),
        ("RouteByProcessingMode", {"Type": "Choice"}),
        ("checkRunPlan", {"Type": "Choice"}),
        ("RuleValidationOrchestration", _TASK),
        ("BDA_InvokeDataAutomation", _TASK),
        ("Shard_ExtractionStep", _TASK),
        ("OcrHookStep", _HOOK),
        ("AfterOcrHook", _HOOK),
        ("ProcessSectionsStep", {"Type": "Map"}),
        ("CheckSections", {"Type": "Map"}),
        ("processSections", {"Type": "Map"}),
        ("PostSectionsHook", {"Type": "Map"}),
        ("FailState", {"Type": "Fail"}),
        ("WorkflowDone", {"Type": "Pass"}),
        ("RecordSkip", {"Type": "Pass"}),
        ("Finished", {"Type": "Succeed"}),
    ],
)
def test_a_name_that_breaks_its_rule_is_reported(name, state):
    assert name_problem(name, state) is not None


@pytest.mark.parametrize(
    "name, state",
    [
        ("PostOcrHook", _HOOK),
        ("PreprocessingHook", _HOOK),
        ("OCRStep", _TASK),
        ("BDA_AssembleDocumentStep", _TASK),
        ("CheckRunPlan", {"Type": "Choice"}),
        ("ValidatePolicyRulesBySection", {"Type": "Map"}),
        ("SectionComplete", {"Type": "Pass"}),
        ("StartAtAssessment", {"Type": "Pass"}),
        ("WorkflowFailed", {"Type": "Fail"}),
    ],
)
def test_a_name_that_fits_its_rule_is_not_reported(name, state):
    assert name_problem(name, state) is None


@pytest.mark.parametrize(
    "name",
    [
        "CheckHITLRequired",
        "Pipeline_ProcessResultsStep",
        "RuleValidationStep",
        "CheckRuleValidationEnabled",
        "SetEmptyRuleValidationResult",
    ],
)
def test_a_retired_word_is_reported(name):
    assert word_problem(name) is not None


@pytest.mark.parametrize(
    "name",
    [
        "PolicyRuleValidationStep",
        "CheckPolicyRuleValidationEnabled",
        "ApplyPostRuleValidationHookDocument",
        "RoutingStep",
    ],
)
def test_an_allowed_form_is_not_reported(name):
    assert word_problem(name) is None


def test_a_mode_prefix_without_its_pair_is_reported():
    names = {bare(qualified) for qualified in STATES}
    assert mode_pair_problems(names - {"BDA_AssembleDocumentStep"}) != []
    assert mode_pair_problems(names - {"Pipeline_AssembleDocumentStep"}) != []
    assert mode_pair_problems(names | {"BDA_InvokeDataAutomationStep"}) != []


def test_a_renamed_state_in_the_definition_is_found():
    """End to end on a copy of the definition, not on a synthetic state."""
    definition = copy.deepcopy(DEFINITION)
    states = definition["States"]
    states["RoutingDecisionChoice"] = states.pop("CheckRoutingDecision")

    problems = {
        qualified: name_problem(bare(qualified), state)
        for qualified, state in all_states(definition).items()
    }

    assert {name for name, problem in problems.items() if problem} == {
        "RoutingDecisionChoice"
    }
