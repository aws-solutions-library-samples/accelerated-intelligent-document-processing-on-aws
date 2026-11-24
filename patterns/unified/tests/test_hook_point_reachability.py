# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which hook states each processing mode reaches, and the table generated from it.

`onError: fail` can only gate a document at a hook point the active processing
mode actually EXECUTES. In BDA mode the state machine has no `postOcr`,
`postClassification` or `postExtraction` state — BDA performs OCR, classification
and extraction inside one Bedrock Data Automation invocation — so a hook
registered at one of those never runs and its fail policy never runs either
(#982). Three components refuse or report such a registration, and all three read
the same GENERATED table:

* `patterns/unified/src/pipeline_hooks_function/hook_point_reachability.py`
* `feature-platform/main-stack-extensions/lambdas/register_feature_hooks/hook_point_reachability.py`
* `lib/idp_common_pkg/idp_common/config/hook_point_reachability.py`

This module is the gate on that table. It re-derives the reachability from
`statemachine/workflow.asl.json` — the only authority — and asserts (1) the exact
per-branch hook-state table, and (2) that every committed copy of the generated
module is byte-identical to a fresh generation. A hardcoded list of three point
names in Python would go stale the moment a hook point is added or moved, which
is the same class of defect as the bug itself; the exact table below also pins the
reachability claim the docs make, so implementing #982's option 3 (mapping the
three points onto BDA's own boundaries) must update both together.

Pure JSON parsing and file reads; nothing here builds an AWS client.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PATTERN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PATTERN_ROOT.parents[1]
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_hook_point_reachability.py"


def _generator():
    """The generator module, loaded by path (scripts/ is not an importable pkg)."""
    spec = importlib.util.spec_from_file_location(
        "generate_hook_point_reachability", GENERATOR_PATH
    )
    assert spec and spec.loader, f"cannot load {GENERATOR_PATH}"
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so a future dataclass/pickle use inside it resolves.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GEN = _generator()
TABLE = GEN.derive(GEN.load_asl())

# The expected table, from issue #982. State names are qualified by the Map that
# contains them, so `PostExtractionHook` appears as
# `ProcessSections.PostExtractionHook` — it lives inside the section Map, which
# the BDA branch never enters.
EXPECTED_ALWAYS = ["PreprocessingHook"]
EXPECTED_BDA = [
    "PostRuleValidationHook",
    "PostSummarizationHook",
    "PostprocessingHook",
    "PreprocessingHook",
]
EXPECTED_PIPELINE = [
    "PostClassificationHook",
    "PostOcrHook",
    "PostRuleValidationHook",
    "PostSummarizationHook",
    "PostprocessingHook",
    "PreprocessingHook",
    "ProcessSections.PostExtractionHook",
]


@pytest.mark.unit
def test_the_mode_router_and_its_two_branch_entries_are_found_by_shape():
    """Non-vacuity guard. Everything below is relative to this split.

    The router is located by the Choice VARIABLE, not by state name, so a rename
    keeps the table honest instead of quietly producing an empty one.
    """
    assert TABLE["router"] == "CheckProcessingMode", (
        f"the Choice switching on {GEN.ROUTER_VARIABLE} is "
        f"{TABLE['router']!r}; if the router was deliberately renamed, update the "
        f"docs that name it as well as this expectation"
    )
    assert TABLE["entries"] == {
        "bda": "CheckExistingBdaData",
        "pipeline": "OCRStep",
    }, (
        f"the branch entry states changed to {TABLE['entries']}. The reachability "
        f"table is derived from these two walks, so the per-mode sets below now "
        f"describe different branches than the docs do."
    )


# ---------------------------------------------------------------------------
# The run-plan dispatcher: the graph starts at a Choice on the plan's start
# ---------------------------------------------------------------------------

#: Entry id -> where a run with that start enters. `beginning` has no rule: it is
#: the dispatcher's Default.
PLAN_ENTRY_TARGETS = {
    "pipeline.classification": "StartAtClassification",
    "pipeline.extraction": "StartAtExtraction",
    "pipeline.extraction_sections": "StartAtExtractionForSections",
    "pipeline.assessment": "StartAtAssessment",
    "apply_review_changes": "ApplyReviewChangesStep",
    "policy_rule_validation": "StartAtPolicyRuleValidation",
    "rejected": "SetRejectedStatus",
}


def _plan_rule(entry: str, target: str) -> dict:
    """A dispatcher rule in the definition's guarded form."""
    return {
        "And": [
            {"Variable": GEN.PLAN_START_VARIABLE, "IsPresent": True},
            {"Variable": GEN.PLAN_START_VARIABLE, "StringEquals": entry},
        ],
        "Next": target,
    }


def _retarget(asl: dict, entry: str, target: str) -> None:
    """Point the dispatcher rule of `entry` at another state."""
    for rule in asl["States"][TABLE["dispatcher"]]["Choices"]:
        if GEN._entry_id(rule) == entry:
            rule["Next"] = target
            return
    raise AssertionError(f"no dispatcher rule for {entry}")


@pytest.mark.unit
def test_the_run_plan_dispatcher_is_found_and_the_walk_starts_at_its_default():
    """Non-vacuity guard for everything the dispatcher cases below assert.

    The dispatcher is located by being `StartAt` and by the variable its rules
    read, not by its name. The table is derived from where a run WITHOUT a plan
    starts, so the rules are not part of any mode's walk.
    """
    assert TABLE["dispatcher"] == "CheckRunPlan"
    assert TABLE["start"] == "PreprocessingHook"
    assert TABLE["plan_entries"] == PLAN_ENTRY_TARGETS


@pytest.mark.unit
def test_the_table_is_the_one_a_graph_without_a_dispatcher_gives():
    """The dispatcher changes where runs can start, not what a mode reaches.

    Deriving from the same graph with `StartAt` set to the dispatcher's Default
    (no dispatcher to find) must give the same per-mode sets, or the dispatcher's
    rules leaked into a mode's walk.
    """
    asl = GEN.load_asl()
    asl["StartAt"] = TABLE["start"]

    plain = GEN.derive(asl)

    assert plain["dispatcher"] is None
    for key in ("always_states", "states_by_mode", "points_by_mode", "all_points"):
        assert plain[key] == TABLE[key], key


@pytest.mark.unit
def test_a_start_entry_that_is_a_hook_state_is_refused():
    asl = GEN.load_asl()
    _retarget(asl, "policy_rule_validation", "PostRuleValidationHook")

    with pytest.raises(SystemExit, match="must not start at a\\s+hook"):
        GEN.derive(asl)


@pytest.mark.unit
def test_a_start_entry_that_can_reach_the_preprocessing_hook_is_refused():
    """Only a run from the beginning passes the hook ahead of the mode choice."""
    asl = GEN.load_asl()
    # Not the hook itself (that is the case above): a state that leads back to it.
    asl["States"]["StartAtClassification"]["Next"] = "PreprocessingHook"

    with pytest.raises(SystemExit, match="only a run from the beginning passes"):
        GEN.derive(asl)


@pytest.mark.unit
def test_a_shared_start_entry_that_reaches_a_pipeline_only_hook_is_refused():
    """An entry both modes use must stay inside the hooks both modes reach.

    `apply_review_changes` runs in BDA mode too. Sent into the Pipeline branch it
    would fire `postClassification` for a BDA configuration, which the table says
    cannot happen.
    """
    asl = GEN.load_asl()
    _retarget(asl, "apply_review_changes", "ClassificationStep")

    with pytest.raises(SystemExit, match="both processing modes reach"):
        GEN.derive(asl)


@pytest.mark.unit
def test_a_pipeline_start_entry_that_reaches_a_bda_only_hook_is_refused():
    """A `pipeline.` entry must stay inside the hooks Pipeline mode reaches."""
    asl = GEN.load_asl()
    states = asl["States"]
    # A hook only the BDA branch has: after the assembly of a document that
    # already has its data.
    states["PostBdaAssemblyHook"] = {
        "Type": "Task",
        "Resource": "arn:${Partition}:states:::lambda:invoke",
        "Parameters": {
            "FunctionName": "${" + GEN.DISPATCHER_SUBSTITUTION + "}",
            "Payload": {"hookPoint": "postBdaAssembly"},
        },
        "Next": states["AssembleExistingDocumentStep"]["Next"],
    }
    states["AssembleExistingDocumentStep"]["Next"] = "PostBdaAssemblyHook"
    # The graph is still derivable: the new hook is a BDA-only point.
    assert "postBdaAssembly" in GEN.derive(asl)["points_by_mode"]["bda"]

    _retarget(asl, "pipeline.assessment", "AssembleExistingDocumentStep")

    with pytest.raises(SystemExit, match="Pipeline mode reaches"):
        GEN.derive(asl)


@pytest.mark.unit
def test_a_start_choice_that_mixes_the_plan_with_another_variable_is_refused():
    """Where a run from the beginning starts would otherwise be a guess."""
    asl = GEN.load_asl()
    asl["States"][TABLE["dispatcher"]]["Choices"].append(
        {"Variable": "$.document.priority", "StringEquals": "high", "Next": "OCRStep"}
    )

    with pytest.raises(SystemExit, match="not purely the\\s+run-plan dispatcher"):
        GEN.derive(asl)


@pytest.mark.unit
def test_a_dispatcher_without_a_default_reports_what_is_missing():
    asl = GEN.load_asl()
    del asl["States"][TABLE["dispatcher"]]["Default"]

    with pytest.raises(SystemExit, match="where a run without a plan starts"):
        GEN.derive(asl)


@pytest.mark.unit
def test_two_rules_for_one_entry_are_refused():
    asl = GEN.load_asl()
    asl["States"][TABLE["dispatcher"]]["Choices"].append(
        _plan_rule("rejected", "WorkflowComplete")
    )

    with pytest.raises(SystemExit, match="two rules for the entry `rejected`"):
        GEN.derive(asl)


@pytest.mark.unit
def test_a_dispatcher_rule_that_names_no_entry_is_refused():
    asl = GEN.load_asl()
    asl["States"][TABLE["dispatcher"]]["Choices"].append(
        {"Variable": GEN.PLAN_START_VARIABLE, "IsPresent": True, "Next": "OCRStep"}
    )

    with pytest.raises(SystemExit, match="entry it stands for cannot be read"):
        GEN.derive(asl)


@pytest.mark.unit
def test_hook_states_reachable_per_branch_match_the_documented_table():
    """The exact per-branch hook-state table, asserted in both directions.

    Equality, not containment: a hook state that appears in a branch it was not
    in is as much a change to the contract as one that disappears. In particular,
    giving the BDA branch its own postOcr-equivalent state — issue #982's option
    3 — lands here, where it must be accompanied by a docs update, rather than
    silently changing what `onError: fail` means in BDA mode.
    """
    assert TABLE["always_states"] == EXPECTED_ALWAYS, (
        f"hook states ahead of {TABLE['router']} are {TABLE['always_states']}, "
        f"expected {EXPECTED_ALWAYS}. The docs tell extension authors to register "
        f"a gating hook at `preprocessing` precisely because it is the one point "
        f"BOTH modes always execute, before the routing decision."
    )
    assert TABLE["states_by_mode"]["bda"] == EXPECTED_BDA, (
        f"BDA-branch hook states are {TABLE['states_by_mode']['bda']}, expected "
        f"{EXPECTED_BDA} (issue #982's table, mirrored in docs/feature-platform.md "
        f"and docs/feature-platform-developer-guide.md)."
    )
    assert TABLE["states_by_mode"]["pipeline"] == EXPECTED_PIPELINE, (
        f"Pipeline-branch hook states are "
        f"{TABLE['states_by_mode']['pipeline']}, expected {EXPECTED_PIPELINE}."
    )


@pytest.mark.unit
def test_the_three_step_specific_points_are_pipeline_only():
    """The same claim at POINT level — what registration and the dispatcher act on.

    The state table above can change shape (a state renamed, a Map introduced)
    without changing which POINTS a mode reaches, and the points are what the
    generated table exports and every consumer keys off.
    """
    step_specific = {"postOcr", "postClassification", "postExtraction"}
    bda = set(TABLE["points_by_mode"]["bda"])
    pipeline = set(TABLE["points_by_mode"]["pipeline"])

    assert not (bda & step_specific), (
        f"the BDA branch now reaches {sorted(bda & step_specific)}. That may be a "
        f"fix for #982 rather than a regression — but the registration-time "
        f"refusal, the dispatcher's runtime report and the mode tables in "
        f"docs/feature-platform.md and docs/feature-platform-developer-guide.md "
        f"all still say these points are never invoked under BDA."
    )
    assert step_specific <= pipeline, (
        f"the Pipeline branch does NOT reach "
        f"{sorted(step_specific - pipeline)}, so those points are dead in BOTH "
        f"modes — a worse fail-open than #982, and the docs are wrong too."
    )
    for point in ("preprocessing", "postRuleValidation", "postSummarization", "postprocessing"):
        assert point in bda and point in pipeline, (
            f"{point} is documented as reachable in BOTH processing modes but is "
            f"reached from {'pipeline only' if point in pipeline else 'BDA only'}. "
            f"An `onError: fail` policy there is inert in the other mode."
        )
    assert bda | pipeline == set(TABLE["all_points"]), (
        "a hook point is reachable from neither branch nor before the router, so "
        "it is dead in both modes"
    )


@pytest.mark.unit
@pytest.mark.parametrize("target", GEN.TARGETS, ids=lambda p: p.parent.name)
def test_every_committed_copy_of_the_generated_table_is_current(target):
    """The generated module must equal a fresh generation, in all three places.

    Three components need this table and none of them can read the ASL at
    runtime: the dispatcher and `register_feature_hooks` are packaged boto3-only
    with the ASL outside their CodeUri, and `idp_common.config` runs in the API
    resolvers and the CLI. So it is generated and committed — and the whole point
    of generating it is lost if a copy can drift, which is what this asserts.
    """
    expected = GEN.render(TABLE)
    path = REPO_ROOT / target
    assert path.exists(), (
        f"{target} is missing. Regenerate with: "
        f"python3 scripts/generate_hook_point_reachability.py"
    )
    assert path.read_text() == expected, (
        f"{target} is stale relative to statemachine/workflow.asl.json. "
        f"Regenerate with: python3 scripts/generate_hook_point_reachability.py"
    )


@pytest.mark.unit
def test_a_back_edge_into_a_pre_router_state_keeps_preprocessing_in_both_modes():
    """"Ahead of the router" must be defined against the ROUTER, not the branches.

    Defining it as "reachable from StartAt minus reachable from either branch"
    collapses the moment any state inside a branch routes back to a pre-router state
    — a `Catch` on `OCRStep` sending a failure to `PreprocessingHook`, say. That
    would empty the always-run set, drop `preprocessing` from both modes' point sets,
    and make the consumers refuse a gating hook at the one point that always runs:
    exactly the PII-redaction registration the docs tell authors to use.
    """
    asl = GEN.load_asl()
    asl["States"]["OCRStep"].setdefault("Catch", []).insert(
        0, {"ErrorEquals": ["States.Timeout"], "Next": "PreprocessingHook"}
    )

    table = GEN.derive(asl)

    assert table["always_states"] == EXPECTED_ALWAYS
    assert "preprocessing" in table["points_by_mode"]["bda"]
    assert "preprocessing" in table["points_by_mode"]["pipeline"]


@pytest.mark.unit
def test_an_inverted_router_is_refused_rather_than_silently_relabelled():
    """The comparator is part of the match, not just the variable.

    Matching on `Variable` alone would read `BooleanEquals: false -> OCRStep` as the
    BDA rule, swap the two labels, and hand BDA mode all seven points — reopening
    #982 with a green generator and green consumers.
    """
    asl = GEN.load_asl()
    router = asl["States"][TABLE["router"]]
    router["Choices"] = [
        {
            "Variable": GEN.ROUTER_VARIABLE,
            "BooleanEquals": False,
            "Next": TABLE["entries"]["pipeline"],
        }
    ]
    router["Default"] = TABLE["entries"]["bda"]

    with pytest.raises(SystemExit, match="BooleanEquals true"):
        GEN.derive(asl)


@pytest.mark.unit
def test_a_second_mode_switching_choice_is_refused_rather_than_adopted():
    """Uniqueness of the router is asserted, not assumed.

    "Run this extra step only in BDA mode" is a plausible future edit, and it adds a
    second Choice switching on the same variable with the same comparator. Taking the
    first match would adopt it as the router if it were ordered earlier — and its two
    sides are not the processing-mode branches, so the table would hand BDA mode the
    three step-specific points and reopen #982 by a different door than the
    comparator assertion closes.
    """
    asl = GEN.load_asl()
    asl["States"]["ExtraBdaOnlyGate"] = {
        "Type": "Choice",
        "Choices": [
            {
                "Variable": GEN.ROUTER_VARIABLE,
                "BooleanEquals": True,
                "Next": TABLE["entries"]["bda"],
            }
        ],
        "Default": TABLE["entries"]["pipeline"],
    }

    with pytest.raises(SystemExit, match="router is ambiguous"):
        GEN.derive(asl)


@pytest.mark.unit
def test_a_router_without_a_default_reports_what_is_missing():
    """A Choice with only explicit rules and no `Default` is legal ASL.

    The Pipeline branch is read from `Default`, so without it the table genuinely
    cannot be derived — but a bare KeyError would say nothing about what to do.
    """
    asl = GEN.load_asl()
    del asl["States"][TABLE["router"]]["Default"]

    with pytest.raises(SystemExit, match="no string `Default`"):
        GEN.derive(asl)


@pytest.mark.unit
def test_the_generated_helper_answers_the_question_consumers_ask():
    """Load the committed dispatcher copy and check its two exported helpers."""
    path = (
        REPO_ROOT
        / "patterns/unified/src/pipeline_hooks_function/hook_point_reachability.py"
    )
    spec = importlib.util.spec_from_file_location("hook_point_reachability_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.unreachable_hook_points(True) == frozenset(
        {"postOcr", "postClassification", "postExtraction"}
    )
    assert module.unreachable_hook_points(False) == frozenset()
    assert module.processing_mode(True) == "bda"
    assert module.processing_mode(False) == "pipeline"
