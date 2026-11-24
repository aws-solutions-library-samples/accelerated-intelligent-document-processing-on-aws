# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How the workflow definition reads the run plan.

The queue processor writes a run plan into the execution input as ``runPlan``
(``idp_common.run_plan``). The definition reads it in four ways: ``CheckRunPlan``
starts the run at the step the plan names, four Choices skip a step the plan
skips, ``CheckReviewToApply`` sends the run of a completed review that extracted
again on to the step that applies the review, and two entry states copy a value
out of it. A run without a plan is valid, so every one of those reads has to be
safe when ``runPlan`` is absent, and a run that starts at a later step has to
find the inputs that step reads.

These are structural assertions on ``statemachine/workflow.asl.json``. Nothing
is executed; what is checked is what Step Functions would otherwise report as
``States.Runtime`` in the first execution that takes the path.
"""

from __future__ import annotations

import copy

import pytest
from _workflow_definition import (
    EXECUTION_INPUT_KEYS,
    GEN,
    PLAN_PATH_PREFIX,
    SHAPES,
    all_states,
    bare,
    first_work_after,
    is_hook,
    load_definition,
    reachable_from,
    strings,
    ways_on,
)

from idp_common.run_plan.asl import (
    ENTRY_BEGINNING,
    ENTRY_IDS,
    SKIP_FLAGS,
    to_execution_input,
)
from idp_common.run_plan.models import RunKind, RunPlan
from idp_common.run_plan.steps import EXTRACTION

DEFINITION = load_definition()
TOP = DEFINITION["States"]
STATES = all_states(DEFINITION)
DISPATCHER = GEN.find_dispatcher(DEFINITION)
PLAN_RULES = GEN.dispatcher_rules(DEFINITION, DISPATCHER) if DISPATCHER else {}

#: The fields of a state that take a path or a block of paths to resolve.
PATH_FIELDS = ("Parameters", "ItemSelector", "InputPath", "ItemsPath", "ResultSelector")

#: The only states whose Parameters read the plan. Each is reached solely
#: through a ``CheckRunPlan`` rule, so the plan exists when it runs.
PLAN_PARAMETER_STATES = {"StartAtExtractionForSections", "StartAtPolicyRuleValidation"}

#: Tasks that get the whole execution input, and the key it arrives under.
EXECUTION_INPUT_TASKS = ("ApplyReviewChangesStep", "RoutingStep", "AssessmentStep")

#: The assembly steps of BDA mode. No run of a completed review passes them: in
#: BDA mode such a run starts at ApplyReviewChangesStep.
BDA_ASSEMBLY_TASKS = ("BDA_AssembleDocumentStep", "AssembleExistingDocumentStep")
ASSEMBLY_TASKS = ("Pipeline_AssembleDocumentStep", *BDA_ASSEMBLY_TASKS)
#: The entries of a run that extracts again: every section, or the named ones.
EXTRACTION_ENTRIES = ("pipeline.extraction", "pipeline.extraction_sections")
NEW_TASKS = ("ApplyReviewChangesStep", "RoutingStep")
TIMEOUT_ERRORS = {"Sandbox.Timedout", "States.Timeout"}
TRANSIENT_ERRORS = {"ThrottlingException", "Lambda.TooManyRequestsException"}


def _state(name: str) -> dict:
    """A state by its own name, wherever it is nested."""
    found = [state for qualified, state in STATES.items() if bare(qualified) == name]
    assert len(found) == 1, f"{name}: {len(found)} states"
    return found[0]


def _plan_reads(rule: dict) -> list[str]:
    """The plan paths a Choice rule reads, in the order it reads them."""
    paths = []
    if str(rule.get("Variable", "")).startswith(PLAN_PATH_PREFIX):
        paths.append(rule["Variable"])
    for key in ("And", "Or"):
        for member in rule.get(key) or []:
            paths += _plan_reads(member)
    if isinstance(rule.get("Not"), dict):
        paths += _plan_reads(rule["Not"])
    return paths


def guard_problem(rule: dict) -> str | None:
    """Why a Choice rule reads the plan unsafely, or ``None``.

    A safe rule is an ``And`` whose first member tests ``IsPresent`` on the path
    every other member reads: a comparison on a path that does not exist fails
    the execution, and a run without a plan has no ``runPlan`` at all.
    """
    reads = _plan_reads(rule)
    if not reads:
        return None
    members = rule.get("And")
    if not isinstance(members, list) or not members:
        return "reads the plan outside an And"
    first = members[0]
    if first.get("IsPresent") is not True:
        return "its first member is not an IsPresent test"
    if set(reads) != {first.get("Variable")}:
        return "it reads a plan path its IsPresent test does not cover"
    return None


def _plan_choice_rules():
    """(state, rule index, rule) for every Choice rule that reads the plan."""
    for qualified, state in STATES.items():
        for index, rule in enumerate(state.get("Choices") or []):
            if _plan_reads(rule):
                yield qualified, index, rule


PLAN_CHOICE_RULES = list(_plan_choice_rules())


def jump_findings(definition: dict, target: str) -> list[str]:
    """What a run that starts at ``target`` would read and not find.

    The shape analysis, started at the target with the execution input as a
    CLOSED seed: at that point ``$`` holds exactly what the queue processor
    wrote, and every other key a later state reads has to be produced on the
    way to it.
    """
    findings: list[str] = []
    SHAPES.analyze(
        {"StartAt": target, "States": definition["States"]},
        SHAPES.Shape(EXECUTION_INPUT_KEYS, closed=True),
        "",
        findings,
    )
    return findings


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


def test_the_definition_starts_at_the_run_plan_dispatcher():
    """Non-vacuity guard: everything below is about this Choice."""
    assert DISPATCHER == DEFINITION["StartAt"] == "CheckRunPlan"
    assert TOP[DISPATCHER]["Type"] == "Choice"


def test_default_is_the_first_step():
    """A run without a plan, and a plan from the beginning, run every step."""
    assert TOP[DISPATCHER]["Default"] == "PreprocessingHook"


def test_there_is_one_rule_for_every_entry_but_the_beginning():
    """The entry ids are declared once, in ``idp_common.run_plan.asl``.

    An entry the queue processor can write and the dispatcher has no rule for
    would run the document from the beginning without anyone asking for that.
    """
    assert len(TOP[DISPATCHER]["Choices"]) == len(PLAN_RULES)
    assert set(PLAN_RULES) == set(ENTRY_IDS) - {ENTRY_BEGINNING}


@pytest.mark.parametrize("entry", sorted(PLAN_RULES))
def test_a_run_never_starts_at_a_hook(entry):
    target = PLAN_RULES[entry]
    assert not is_hook(TOP[target]), f"{entry} starts at the hook state {target}"


@pytest.mark.parametrize("entry", sorted(PLAN_RULES))
def test_only_a_run_from_the_beginning_passes_the_preprocessing_hook(entry):
    """A run that starts later works on a document that hook has already seen."""
    assert "PreprocessingHook" not in reachable_from(TOP, PLAN_RULES[entry])


@pytest.mark.parametrize("entry", sorted(PLAN_RULES))
def test_jump_targets_have_their_inputs(entry):
    """Every state a planned run reaches reads only keys the run produced."""
    findings = jump_findings(DEFINITION, PLAN_RULES[entry])
    assert not findings, (
        f"a run that starts at {entry} ({PLAN_RULES[entry]}) reads keys nothing on "
        f"its path produces:\n  " + "\n  ".join(findings)
    )


def test_rejected_path_has_no_task():
    """The run of a rejected review ends without calling a Lambda."""
    path = reachable_from(TOP, PLAN_RULES["rejected"])
    work = sorted(name for name in path if TOP[name]["Type"] in ("Task", "Map"))
    assert work == []
    assert any(TOP[name].get("End") for name in path), "the path does not end"


# ---------------------------------------------------------------------------
# Reads of the plan
# ---------------------------------------------------------------------------


def test_the_definition_reads_the_plan_in_choices():
    """Non-vacuity guard for the guard test below."""
    readers = {bare(qualified) for qualified, _, _ in PLAN_CHOICE_RULES}
    assert readers == {
        "CheckRunPlan",
        "CheckAssessmentSkipped",
        "CheckReviewToApply",
        "CheckPolicyRuleValidationEnabled",
        "CheckSummarizationSkipped",
        "CheckEvaluationSkipped",
    }


@pytest.mark.parametrize(
    "qualified, index, rule",
    PLAN_CHOICE_RULES,
    ids=[f"{bare(q)}[{i}]" for q, i, _ in PLAN_CHOICE_RULES],
)
def test_a_choice_tests_the_plan_path_is_present_before_reading_it(
    qualified, index, rule
):
    problem = guard_problem(rule)
    assert problem is None, f"{qualified} rule {index}: {problem}"


def test_only_the_two_entry_states_read_the_plan_in_a_path_field():
    """Everything else takes ``$$.Execution.Input``, which always exists."""
    readers = {
        bare(qualified)
        for qualified, state in STATES.items()
        for field in PATH_FIELDS
        if any(PLAN_PATH_PREFIX in value for value in strings(state.get(field)))
    }
    assert readers == PLAN_PARAMETER_STATES


@pytest.mark.parametrize("name", sorted(PLAN_PARAMETER_STATES))
def test_a_state_that_reads_the_plan_in_parameters_is_reached_only_by_a_plan(name):
    """Its only way in is a dispatcher rule, so the plan path exists there."""
    into = {
        bare(qualified) for qualified, state in STATES.items() if name in ways_on(state)
    }
    assert into == {DISPATCHER}
    assert name in PLAN_RULES.values()


def test_every_plan_key_the_definition_reads_is_one_the_queue_processor_writes():
    """The definition and ``to_execution_input`` agree on the block's keys."""
    read = {
        value[len(PLAN_PATH_PREFIX) :].split(".")[0].split("[")[0]
        for state in STATES.values()
        for value in strings(state)
        if value.startswith(PLAN_PATH_PREFIX)
    }
    written = set(
        to_execution_input(
            RunPlan(kind="reprocess", start="beginning"),
            mode="pipeline",
            policy_rule_validation_enabled=True,
        )
    )
    assert read, "the definition reads no plan key"
    assert read <= written, f"read but never written: {sorted(read - written)}"
    # Every skip flag the plan can carry is acted on.
    assert set(SKIP_FLAGS.values()) <= read


# ---------------------------------------------------------------------------
# A step the plan skips fires no hook
# ---------------------------------------------------------------------------


def test_skipping_policy_rule_validation_goes_straight_to_routing():
    assert first_work_after(TOP, "SkipPolicyRuleValidation") == {"RoutingStep"}


def test_skipping_summarization_bypasses_its_hook():
    assert "PostSummarizationHook" not in reachable_from(TOP, "SkipSummarization")
    assert TOP["CheckSummarizationSkipped"]["Default"] == "SummarizationStep"


def test_skipping_evaluation_goes_to_the_postprocessing_hook():
    """That hook belongs to the end of the run, not to evaluation."""
    assert TOP["SkipEvaluation"]["Next"] == "PostprocessingHook"
    assert "EvaluationStep" not in reachable_from(TOP, "SkipEvaluation")


def test_skipping_assessment_completes_the_section():
    choice = _state("CheckAssessmentSkipped")
    assert [rule["Next"] for rule in choice["Choices"]] == ["SectionComplete"]
    assert choice["Default"] == "AssessmentStep"


def test_a_step_the_configuration_turns_off_still_fires_its_hook():
    """Only a plan skip bypasses the hook; a disabled step keeps it."""
    assert TOP["CheckPolicyRuleValidationEnabled"]["Default"] == (
        "SetEmptyPolicyRuleValidationResult"
    )
    assert first_work_after(TOP, "SetEmptyPolicyRuleValidationResult") == {
        "PostRuleValidationHook"
    }


# ---------------------------------------------------------------------------
# What the handlers are sent
# ---------------------------------------------------------------------------

HOOK_STATES = sorted(q for q, state in STATES.items() if is_hook(state))


def test_the_hook_states_are_found():
    assert len(HOOK_STATES) == len(GEN.hook_states(DEFINITION)) >= 7


@pytest.mark.parametrize("qualified", HOOK_STATES)
def test_every_hook_is_told_about_the_run(qualified):
    payload = STATES[qualified]["Parameters"]["Payload"]
    assert payload.get("executionInput.$") == "$$.Execution.Input"


@pytest.mark.parametrize("name", EXECUTION_INPUT_TASKS)
def test_the_steps_that_read_the_plan_get_the_execution_input(name):
    assert _state(name)["Parameters"].get("execution_input.$") == "$$.Execution.Input"


@pytest.mark.parametrize("name", ASSEMBLY_TASKS)
def test_the_assembly_steps_say_the_definition_has_a_routing_step(name):
    """The literal that tells the handler not to make the review decision."""
    assert _state(name)["Parameters"].get("routing_step") is True


@pytest.mark.parametrize("name", BDA_ASSEMBLY_TASKS)
def test_the_bda_assembly_steps_go_on_to_policy_rule_validation(name):
    assert _state(name)["Next"] == "CheckPolicyRuleValidationEnabled"


def test_the_pipeline_assembly_step_goes_on_to_the_review_check():
    assert _state("Pipeline_AssembleDocumentStep")["Next"] == "CheckReviewToApply"


# ---------------------------------------------------------------------------
# A completed review whose run extracts again: the review is applied after it
# ---------------------------------------------------------------------------


def _completed_review_plan(sections: list[str]) -> dict:
    """The ``runPlan`` block of a completed review that extracts again."""
    return to_execution_input(
        RunPlan(
            kind=RunKind.REVIEW_COMPLETED,
            start=EXTRACTION,
            sections=sections,
            cycle_id="0123456789abcdef01234",
        ),
        mode="pipeline",
        policy_rule_validation_enabled=True,
    )


def test_the_review_check_sends_a_completed_review_on_to_apply_it():
    """One rule, on the plan's kind, and every other run takes the Default."""
    choice = TOP["CheckReviewToApply"]
    assert choice["Type"] == "Choice"
    assert choice["Choices"] == [
        {
            "And": [
                {"Variable": f"{PLAN_PATH_PREFIX}kind", "IsPresent": True},
                {
                    "Variable": f"{PLAN_PATH_PREFIX}kind",
                    "StringEquals": "review_completed",
                },
            ],
            "Next": "SelectAssembledDocument",
        }
    ]
    assert choice["Default"] == "CheckPolicyRuleValidationEnabled"


@pytest.mark.parametrize("sections", [["2"], []], ids=["named", "every-section"])
def test_the_review_check_compares_with_the_kind_the_plan_carries(sections):
    """The value in the rule is the one the queue processor writes."""
    block = _completed_review_plan(sections)
    [rule] = TOP["CheckReviewToApply"]["Choices"]

    assert block["startAt"] in EXTRACTION_ENTRIES
    assert block["kind"] == rule["And"][1]["StringEquals"]


def test_the_assembled_document_is_put_where_apply_review_changes_reads_it():
    state = TOP["SelectAssembledDocument"]
    assert state["Type"] == "Pass"
    # What the assembly step returned...
    assert TOP["Pipeline_AssembleDocumentStep"]["ResultPath"] == "$.Result"
    assert state["InputPath"] == "$.Result.document"
    # ...copied to the key the step reads, with the rest of the state kept.
    assert state["ResultPath"] == "$.document"
    assert TOP["ApplyReviewChangesStep"]["Parameters"]["document.$"] == "$.document"
    assert "Parameters" not in state and "Result" not in state
    assert state["Next"] == "ApplyReviewChangesStep"


def test_only_the_review_check_leads_to_select_assembled_document():
    into = {
        bare(qualified)
        for qualified, state in STATES.items()
        if "SelectAssembledDocument" in ways_on(state)
    }
    assert into == {"CheckReviewToApply"}


@pytest.mark.parametrize("entry", EXTRACTION_ENTRIES)
def test_a_run_that_extracts_again_reaches_apply_review_after_the_extraction(entry):
    """Extraction and its hook are in the section Map; the review comes after."""
    assert "ProcessSections.PostExtractionHook" in STATES
    assert first_work_after(TOP, PLAN_RULES[entry]) == {"ProcessSections"}
    assert first_work_after(TOP, "ProcessSections") == {"Pipeline_AssembleDocumentStep"}

    after_the_assembly = reachable_from(TOP, "Pipeline_AssembleDocumentStep")
    assert "ApplyReviewChangesStep" in after_the_assembly
    # There is no way back into the Map: the review is applied once, after it.
    assert "ProcessSections" not in after_the_assembly
    assert first_work_after(TOP, "SelectAssembledDocument") == {
        "ApplyReviewChangesStep"
    }


def test_apply_review_changes_goes_on_to_policy_rule_validation():
    assert TOP["ApplyReviewChangesStep"]["Next"] == "CheckPolicyRuleValidationEnabled"


def test_with_policy_rule_validation_off_the_review_run_still_reaches_routing():
    """ApplyReviewChangesStep answers ``rule_validation_enabled`` false."""
    off = TOP["CheckPolicyRuleValidationEnabled"]["Default"]
    path = reachable_from(TOP, off)

    assert "RoutingStep" in path
    assert "PolicyClassificationStep" not in path
    assert "ApplyReviewChangesStep" not in path


# ---------------------------------------------------------------------------
# The two new steps
# ---------------------------------------------------------------------------


def test_routing_has_no_catch():
    """A document whose routing cannot be decided must not go on unreviewed."""
    assert "Catch" not in TOP["RoutingStep"]


def test_routing_reads_the_document_after_policy_rule_validation():
    assert TOP["RoutingStep"]["Parameters"]["document.$"] == (
        "$.RuleValidationOrchestrationResult.document"
    )
    assert first_work_after(TOP, "PostRuleValidationHook") == {"RoutingStep"}


@pytest.mark.parametrize("name", NEW_TASKS)
def test_a_new_step_retries_a_timeout_once(name):
    retriers = [r for r in TOP[name]["Retry"] if set(r["ErrorEquals"]) & TIMEOUT_ERRORS]
    assert retriers, f"{name} does not name its timeout codes"
    assert all(r["MaxAttempts"] <= 1 for r in retriers)
    assert all(set(r["ErrorEquals"]) <= TIMEOUT_ERRORS for r in retriers)


@pytest.mark.parametrize("name", NEW_TASKS)
def test_a_new_step_retries_a_transient_failure_under_the_handlers_name(name):
    retriers = [
        r for r in TOP[name]["Retry"] if set(r["ErrorEquals"]) & TRANSIENT_ERRORS
    ]
    assert len(retriers) == 1
    assert retriers[0]["MaxAttempts"] >= 3
    assert "TransientError" in retriers[0]["ErrorEquals"]


# ---------------------------------------------------------------------------
# The checks fire: each one on a definition that breaks it
# ---------------------------------------------------------------------------


def _retarget(definition: dict, entry: str, target: str) -> None:
    for rule in definition["States"][DISPATCHER]["Choices"]:
        if GEN._entry_id(rule) == entry:
            rule["Next"] = target
            return
    raise AssertionError(f"no dispatcher rule for {entry}")


def test_an_entry_that_skips_the_state_that_builds_its_inputs_is_found():
    """Starting at ClassificationStep itself: nothing put the document in
    ``$.OCRResult``, which is what ``StartAtClassification`` is for."""
    definition = copy.deepcopy(DEFINITION)
    _retarget(definition, "pipeline.classification", "ClassificationStep")

    findings = jump_findings(definition, "ClassificationStep")

    assert any("ClassificationStep reads $.OCRResult" in f for f in findings), findings


def test_a_review_document_read_from_a_key_the_assembly_does_not_write_is_found():
    definition = copy.deepcopy(DEFINITION)
    definition["States"]["SelectAssembledDocument"]["InputPath"] = "$.Assembled"

    findings = jump_findings(definition, PLAN_RULES["pipeline.extraction_sections"])

    assert any("SelectAssembledDocument reads $.Assembled" in f for f in findings), (
        findings
    )


def test_an_entry_into_the_section_map_without_its_section_list_is_found():
    definition = copy.deepcopy(DEFINITION)

    findings = jump_findings(definition, "ProcessSections")

    assert any("ProcessSections reads $.SectionItems" in f for f in findings), findings


@pytest.mark.parametrize(
    "rule, expected",
    [
        (
            {
                "Variable": "$$.Execution.Input.runPlan.skipEvaluation",
                "BooleanEquals": True,
            },
            "outside an And",
        ),
        (
            {
                "And": [
                    {
                        "Variable": "$$.Execution.Input.runPlan.skipEvaluation",
                        "BooleanEquals": True,
                    }
                ]
            },
            "not an IsPresent test",
        ),
        (
            {
                "And": [
                    {
                        "Variable": "$$.Execution.Input.runPlan.skipEvaluation",
                        "BooleanEquals": True,
                    },
                    {
                        "Variable": "$$.Execution.Input.runPlan.skipEvaluation",
                        "IsPresent": True,
                    },
                ]
            },
            "not an IsPresent test",
        ),
        (
            {
                "And": [
                    {
                        "Variable": "$$.Execution.Input.runPlan.startAt",
                        "IsPresent": True,
                    },
                    {
                        "Variable": "$$.Execution.Input.runPlan.skipEvaluation",
                        "BooleanEquals": True,
                    },
                ]
            },
            "does not cover",
        ),
        (
            {
                "And": [
                    {
                        "Variable": "$$.Execution.Input.runPlan.skipEvaluation",
                        "IsPresent": False,
                    },
                    {
                        "Variable": "$$.Execution.Input.runPlan.skipEvaluation",
                        "BooleanEquals": True,
                    },
                ]
            },
            "not an IsPresent test",
        ),
    ],
)
def test_an_unguarded_plan_read_is_reported(rule, expected):
    assert expected in (guard_problem(rule) or "")


def test_a_rule_that_does_not_read_the_plan_needs_no_guard():
    assert guard_problem({"Variable": "$.Result.x", "BooleanEquals": True}) is None
