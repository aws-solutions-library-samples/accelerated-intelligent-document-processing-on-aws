# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evaluation and escalation are their own Bedrock demand lines (#1953).

Before this, the planner modelled no evaluation demand at all, and an escalation
request — recorded under `ExtractionEscalation/bedrock/<escalation model>` —
satisfied the Extraction step's `"extraction" in key` filter, so it was counted
as an extraction request against the extraction model's quota.

Every test drives the real planner (`build_simple_quota_requirements`, or
`lambda_handler` for the request wiring) against moto's DynamoDB. The metering
contexts are read out of the `idp_common` source that writes them, so a rename
there fails here rather than leaving these fixtures describing keys nothing
produces.
"""

import ast
import json
from pathlib import Path

import index
import pytest
import test_capacity_metrics_and_handler as base

# The fixtures this file shares with the handler tests, bound as module
# attributes so pytest finds them here.
aws = base.aws
tracking = base.tracking
latency_windows = base.latency_windows
wired = base.wired

HAPPY_INPUT = base.HAPPY_INPUT
bedrock = base.bedrock
by_type = base.by_type
hours = base.hours
model_config = base.model_config
put_metered = base.put_metered

REPO = Path(__file__).resolve().parents[3]


def _string_constant(relative_path, name):
    tree = ast.parse((REPO / relative_path).read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    raise AssertionError(f"{name} not found in {relative_path}")


ESCALATION_CONTEXT = _string_constant(
    "lib/idp_common_pkg/idp_common/extraction/service.py",
    "ESCALATION_METERING_CONTEXT",
)
EVALUATION_CONTEXT = _string_constant(
    "lib/idp_common_pkg/idp_common/evaluation/stickler_backend/comparators.py",
    "EVALUATION_METERING_CONTEXT",
)
# The Simple-mode spelling earlier releases stored; the constant's own docstring
# says readers must accept both.
LEGACY_ESCALATION_CONTEXT = "Extraction-Escalation"

BASE = "us.amazon.nova-lite-v1:0"
STRONG = "us.anthropic.claude-sonnet-5"
JUDGE = "us.amazon.nova-2-lite-v1:0"


def quotas(*models, tpm=10_000_000, rpm=100_000):
    return {
        "bedrock_models": {m: tpm for m in models},
        "bedrock_models_rpm": {m: rpm for m in models},
        "unreadable_models": {},
    }


def entry(context, model, requests, tokens):
    return {
        f"{context}/bedrock/{model}": {
            "requests": requests,
            "inputTokens": tokens - 100,
            "outputTokens": 100,
            "totalTokens": tokens,
        }
    }


def plan(hourly, config, *, rate=None, notes=None):
    return index.build_simple_quota_requirements(
        1000,
        50000,
        100,
        quotas(BASE, STRONG, JUDGE),
        600.0,
        "pattern-2",
        config,
        hourly,
        None,
        None,
        True,
        escalation_rate=rate,
        demand_notes=notes,
    )


def test_the_metering_contexts_are_the_ones_the_planner_matches():
    """The fixtures' keys are the shapes the planner's prefixes recognise."""
    assert f"{ESCALATION_CONTEXT.lower()}/" in index.ESCALATION_METERING_PREFIXES
    assert f"{LEGACY_ESCALATION_CONTEXT.lower()}/" in index.ESCALATION_METERING_PREFIXES
    assert index.EVALUATION_METERING_PREFIX == f"{EVALUATION_CONTEXT.lower()}/"


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_evaluation_disabled_plans_no_evaluation_line(tracking):
    """No `evaluation_model` is how "evaluation is off" arrives; nothing is planned
    for it even with evaluation demand in the schedule and in the metering."""
    put_metered(tracking, "doc-1", bedrock("Extraction", 2, model=BASE))
    put_metered(tracking, "doc-2", entry(EVALUATION_CONTEXT, JUDGE, 4, 2000))
    hourly = hours(
        **{
            "9": {
                "docsPerHour": 60,
                "extractionTokensPerHour": 60000,
                "evaluationTokensPerHour": 72000,
                "evaluationComparisonsPerHour": 180,
            }
        }
    )
    rows = plan(hourly, model_config(extraction_model=BASE))
    assert {r["usedFor"] for r in rows} == {"Extraction"}
    assert all(r["modelId"] != JUDGE for r in rows)


@pytest.mark.unit
def test_evaluation_with_no_metering_is_derived_from_the_llm_comparison_count(
    tracking,
):
    """3 LLM comparisons per document x 60 documents is 180 judge requests in the
    hour: 3/min, 3.3 with the buffer, reported as 3. Tokens are the schedule's
    evaluation tokens: 72,000/h is 1,200/min, 1,320 with the buffer. Both rows
    are charged to the judge model, and the note says the figures were derived."""
    put_metered(tracking, "doc-1", bedrock("Extraction", 2, model=BASE))
    hourly = hours(
        **{
            "9": {
                "docsPerHour": 60,
                "extractionTokensPerHour": 60000,
                "evaluationTokensPerHour": 72000,
                "evaluationComparisonsPerHour": 180,
            }
        }
    )
    notes = []
    rows = by_type(
        plan(
            hourly,
            model_config(extraction_model=BASE, evaluation_model=JUDGE),
            notes=notes,
        )
    )
    assert rows[("Evaluation", "TPM")]["modelId"] == JUDGE
    assert rows[("Evaluation", "TPM")]["requiredQuota"] == "1,320"
    assert rows[("Evaluation", "RPM")]["requiredQuota"] == "3"
    assert rows[("Evaluation", "RPM")]["modelId"] == JUDGE
    (note,) = [n for n in notes if n.startswith("Evaluation")]
    assert "derived from the evaluation configuration" in note
    assert JUDGE in note


@pytest.mark.unit
def test_measured_evaluation_requests_replace_the_derived_count(tracking):
    """Two evaluated documents at 4 and 8 judge calls average 6 per document;
    x 60 docs / 60 x 1.1 = 6.6 -> 7. The derived count (1 per document here)
    would have given 1."""
    put_metered(tracking, "doc-1", entry(EVALUATION_CONTEXT, JUDGE, 4, 2000))
    put_metered(tracking, "doc-2", entry(EVALUATION_CONTEXT, JUDGE, 8, 4000))
    hourly = hours(
        **{
            "9": {
                "docsPerHour": 60,
                "evaluationTokensPerHour": 24000,
                "evaluationComparisonsPerHour": 60,
            }
        }
    )
    notes = []
    rows = by_type(plan(hourly, model_config(evaluation_model=JUDGE), notes=notes))
    assert rows[("Evaluation", "RPM")]["requiredQuota"] == "7"
    (note,) = [n for n in notes if n.startswith("Evaluation")]
    assert "measured from the metering of 2 evaluated documents" in note


@pytest.mark.unit
def test_the_comparison_count_does_not_stand_in_for_another_steps_requests(tracking):
    """Only evaluation falls back to the configured comparison count. Extraction
    with demand and no recorded requests still refuses the report."""
    hourly = hours(
        **{
            "9": {
                "docsPerHour": 60,
                "extractionTokensPerHour": 60000,
                "evaluationTokensPerHour": 72000,
                "evaluationComparisonsPerHour": 180,
            }
        }
    )
    with pytest.raises(ValueError, match="No request count data found for Extraction"):
        plan(hourly, model_config(extraction_model=BASE, evaluation_model=JUDGE))


@pytest.mark.unit
def test_evaluation_requests_are_not_counted_as_any_processing_step(tracking):
    """An `Evaluation/` key belongs to the evaluation line only."""
    metering = bedrock("Extraction", 2, model=BASE)
    metering.update(entry(EVALUATION_CONTEXT, JUDGE, 50, 2000))
    put_metered(tracking, "doc-1", metering)
    hourly = hours(**{"9": {"docsPerHour": 60, "extractionTokensPerHour": 60000}})
    rows = by_type(plan(hourly, model_config(extraction_model=BASE)))
    # 2 req/doc x 60 / 60 x 1.1 = 2.2 -> 2
    assert rows[("Extraction", "RPM")]["requiredQuota"] == "2"


# --------------------------------------------------------------------------
# Escalation
# --------------------------------------------------------------------------


def escalation_history(
    table,
    *,
    documents,
    escalated,
    base_tokens=1000,
    esc_tokens=500,
    esc_requests=1,
    context=ESCALATION_CONTEXT,
):
    """`documents` extracted documents, the first `escalated` of which escalated."""
    for i in range(documents):
        metering = entry("Extraction", BASE, 2, base_tokens)
        if i < escalated:
            metering.update(entry(context, STRONG, esc_requests, esc_tokens))
        put_metered(table, f"doc-{i}", metering)


ESCALATING = model_config(extraction_model=BASE, escalation_model=STRONG)
EXTRACTION_HOUR = hours(
    **{"9": {"docsPerHour": 600, "extractionTokensPerHour": 600000}}
)


@pytest.mark.unit
def test_escalation_is_charged_to_the_escalation_model_not_the_extraction_model(
    tracking,
):
    """Each escalation request lands on the escalation line, on the escalation
    model, and the extraction line counts only extraction's own requests:
    2 req/doc x 600 / 60 x 1.1 = 22. Before the fix it read 2 + 1 x (5 of 20)
    escalated = 2.25 req/doc on the base model."""
    escalation_history(tracking, documents=20, escalated=5)
    rows = by_type(plan(EXTRACTION_HOUR, ESCALATING))
    assert rows[("Extraction", "RPM")]["requiredQuota"] == "22"
    assert rows[("Extraction", "RPM")]["modelId"] == BASE
    assert rows[("Escalation", "RPM")]["modelId"] == STRONG
    assert rows[("Escalation", "TPM")]["modelId"] == STRONG


@pytest.mark.unit
def test_a_large_enough_sample_uses_the_measured_escalation_rate(tracking):
    """20 documents, 5 escalated once at 500 tokens over 1,000-token extractions.

    Requests: 5 / 20 = 0.25 per document x 600 / 60 x 1.1 = 2.75 -> 3.
    Tokens: 2,500 escalation over 20,000 extraction tokens is a 12.5% share of
    extraction's 11,000/min peak = 1,375. The default 15% would give 1,650.
    """
    escalation_history(tracking, documents=20, escalated=5)
    notes = []
    rows = by_type(plan(EXTRACTION_HOUR, ESCALATING, notes=notes))
    assert rows[("Escalation", "TPM")]["requiredQuota"] == "1,375"
    assert rows[("Escalation", "RPM")]["requiredQuota"] == "3"
    (note,) = [n for n in notes if n.startswith("Escalation")]
    assert "measured from metering: 5 of 20 sampled documents escalated (25.0%)" in note


@pytest.mark.unit
def test_the_legacy_simple_mode_spelling_is_measured_as_escalation(tracking):
    escalation_history(
        tracking, documents=20, escalated=5, context=LEGACY_ESCALATION_CONTEXT
    )
    rows = by_type(plan(EXTRACTION_HOUR, ESCALATING))
    assert rows[("Escalation", "TPM")]["requiredQuota"] == "1,375"
    assert rows[("Extraction", "RPM")]["requiredQuota"] == "22"


@pytest.mark.unit
def test_measured_requests_per_escalated_document_are_counted_in_full(tracking):
    """An agentic escalation that took 3 calls: 5 x 3 / 20 = 0.75 per document
    x 600 / 60 x 1.1 = 8.25 -> 8."""
    escalation_history(tracking, documents=20, escalated=5, esc_requests=3)
    rows = by_type(plan(EXTRACTION_HOUR, ESCALATING))
    assert rows[("Escalation", "RPM")]["requiredQuota"] == "8"


@pytest.mark.unit
def test_a_sample_one_document_short_falls_back_to_the_default_rate(tracking):
    """19 documents: the default 15%. Tokens 0.15 x 11,000 = 1,650; requests
    0.15 x 1 x 600 / 60 x 1.1 = 1.65 -> 2. The note names the default, its value
    and why the measured rate was not used."""
    escalation_history(tracking, documents=19, escalated=5)
    notes = []
    rows = by_type(plan(EXTRACTION_HOUR, ESCALATING, notes=notes))
    assert index.MIN_ESCALATION_SAMPLE_DOCUMENTS == 20
    assert index.DEFAULT_ESCALATION_RATE == 0.15
    assert rows[("Escalation", "TPM")]["requiredQuota"] == "1,650"
    assert rows[("Escalation", "RPM")]["requiredQuota"] == "2"
    (note,) = [n for n in notes if n.startswith("Escalation")]
    assert "the default rate of 15.0%" in note
    assert "only 19 sampled documents" in note


@pytest.mark.unit
def test_a_large_sample_with_no_escalation_is_not_planned_as_zero(tracking):
    escalation_history(tracking, documents=25, escalated=0)
    notes = []
    rows = by_type(plan(EXTRACTION_HOUR, ESCALATING, notes=notes))
    assert rows[("Escalation", "TPM")]["requiredQuota"] == "1,650"
    (note,) = [n for n in notes if n.startswith("Escalation")]
    assert "none of the 25 sampled documents escalated" in note


@pytest.mark.unit
def test_a_requested_rate_replaces_the_default_but_not_a_measurement(tracking):
    """30%: tokens 0.3 x 11,000 = 3,300. With a measured sample it is ignored."""
    escalation_history(tracking, documents=5, escalated=0)
    notes = []
    rows = by_type(plan(EXTRACTION_HOUR, ESCALATING, rate=0.3, notes=notes))
    assert rows[("Escalation", "TPM")]["requiredQuota"] == "3,300"
    assert "the requested rate of 30.0%" in notes[0]

    escalation_history(tracking, documents=20, escalated=5)
    rows = by_type(plan(EXTRACTION_HOUR, ESCALATING, rate=0.3))
    assert rows[("Escalation", "TPM")]["requiredQuota"] == "1,375"


@pytest.mark.unit
def test_no_escalation_model_plans_no_escalation_line(tracking):
    escalation_history(tracking, documents=20, escalated=5)
    rows = by_type(plan(EXTRACTION_HOUR, model_config(extraction_model=BASE)))
    assert {k[0] for k in rows} == {"Extraction"}


# --------------------------------------------------------------------------
# The request, end to end through lambda_handler
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_handler_plans_both_lines_and_says_which_rate_it_used(wired, monkeypatch):
    """`evaluationComparisons` per document type reaches the evaluation line at
    `DEFAULT_EVALUATION_TOKENS_PER_COMPARISON` tokens each, the requested
    `escalationRate` reaches the escalation line, and the recommendations state
    the basis of each."""
    monkeypatch.setattr(
        index, "get_simple_quotas", lambda *_a: quotas(BASE, STRONG, JUDGE)
    )
    payload = json.loads(json.dumps(HAPPY_INPUT, default=str))
    payload["documentConfigs"][0]["evaluationComparisons"] = 3
    payload["escalationRate"] = 0.2
    user = json.loads(payload["userConfig"])
    user.update(evaluation_model=JUDGE, escalation_model=STRONG)
    payload["userConfig"] = json.dumps(user)
    result = index.lambda_handler(payload, None)
    assert result["success"] is True, result
    rows = by_type(result["quotaRequirements"])
    # 3 x 400 tokens x 60 docs / 60 x 1.1 = 1,320; 180 requests / 60 x 1.1 -> 3
    assert index.DEFAULT_EVALUATION_TOKENS_PER_COMPARISON == 400
    assert rows[("Evaluation", "TPM")]["requiredQuota"] == "1,320"
    assert rows[("Evaluation", "RPM")]["requiredQuota"] == "3"
    # 0.2 x (4,000 x 60 / 60 x 1.1 = 4,400) = 880
    assert rows[("Escalation", "TPM")]["requiredQuota"] == "880"
    assert rows[("Escalation", "TPM")]["modelId"] == STRONG
    assert any("the requested rate of 20.0%" in r for r in result["recommendations"])
    assert any(r.startswith("Evaluation demand") for r in result["recommendations"])


@pytest.mark.unit
@pytest.mark.parametrize("rate", [-0.1, 1.5, "lots"])
def test_an_escalation_rate_outside_zero_to_one_is_refused(wired, rate):
    payload = dict(HAPPY_INPUT, escalationRate=rate)
    result = index.lambda_handler(payload, None)
    assert result["success"] is False
    assert "escalationRate" in result["errorMessage"]
