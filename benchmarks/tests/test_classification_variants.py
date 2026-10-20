# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The classification-configuration axis of ``run_classification_bench`` (#1513).

What this is for
----------------
The driver compares classification **configurations** — combinations of
``granularity``, ``modality`` and ``strategy`` — as well as models. The failure
mode that matters is not a crash: it is a cell that launches, drains, scores and
reports a perfectly normal-looking number **for the wrong configuration**,
because a setting did not survive the merge with the system defaults. That
result is indistinguishable from a real one saying the two settings perform
identically, which is exactly the question the run is asking.

``assert_variant_merged`` is the guard against it, and a guard is only worth
having if it actually fires. So this suite drives it in both directions:

* every variant in :data:`VARIANTS` merges to a config that genuinely carries
  what the variant's name claims, including the **prompts** — packet granularity
  reads ``classification.packet.task_prompt`` and must get one asking for a
  ``segments`` array, because the page prompt asks for a different output shape
  and is read by a different parser;
* a config with each setting knocked back out is **refused**, naming the
  setting. A guard that returns no problems for a broken config is worse than no
  guard, since the run then looks checked.

Two further things are pinned because they are cheap to lose and expensive to
notice:

* **Modality is set explicitly, never left empty.** An unset flag is inferred
  from the prompt's placeholders, which is the right default for a user and the
  wrong one for a measurement — the cell would describe whatever the prompt
  happens to say rather than the combination its name claims.
* **The cost filter covers all three metering contexts.** ``single_pass`` bills
  under ``Classification/`` while ``two_stage`` bills under ``Segmentation/``
  and ``Labeling/`` and emits no ``Classification/`` entry at all. Filtering on
  ``Classification/`` alone — which the driver did — matched nothing for a
  two-stage run and returned a cost of ``0.0``, making the configuration with an
  extra call per segment look like the free one.
"""

from __future__ import annotations

import copy

import pytest
from harness_import import harness_module

bench = harness_module("run_classification_bench")

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def docsplit_base() -> str:
    return bench.TESTSETS["docsplit"]


@pytest.fixture(scope="module")
def a_model() -> str:
    return bench.MODELS["nova2lite"]


def _config(docsplit_base, a_model, variant, mode="topk"):
    return bench.build_config(docsplit_base, a_model, mode, 3, variant=variant)


# --------------------------------------------------------------------------
# Every variant really is what its name says
# --------------------------------------------------------------------------


def test_the_variant_table_is_not_empty():
    """Non-vacuity: every test below is parametrised over it."""
    assert bench.VARIANTS, "VARIANTS is empty, so the parametrised tests assert nothing"


def test_the_shipped_default_is_available_as_a_baseline():
    """A cell with no baseline beside it measures a number rather than a
    difference, so the baseline has to be nameable."""
    assert "page-both" in bench.VARIANTS
    spec = bench.VARIANTS["page-both"]
    assert spec["granularity"] == "page"
    assert spec["text"] is True and spec["images"] is True
    assert spec.get("strategy", "single_pass") == "single_pass"


@pytest.mark.parametrize("variant", sorted(bench.VARIANTS))
def test_every_variant_merges_to_what_it_claims(docsplit_base, a_model, variant):
    cfg = _config(docsplit_base, a_model, variant)
    assert bench.assert_variant_merged(cfg, variant, "topk") == []


@pytest.mark.parametrize("variant", sorted(bench.VARIANTS))
def test_every_variant_sets_modality_explicitly(docsplit_base, a_model, variant):
    """⚠️ Not left empty. An unset flag is inferred from the prompt's
    placeholders — correct for a user, wrong for a measurement, because the cell
    would then describe the prompt rather than the combination it is named for."""
    cfg = _config(docsplit_base, a_model, variant)
    modality = cfg["classification"]["modality"]
    assert isinstance(modality.get("text"), bool)
    assert isinstance(modality.get("images"), bool)


@pytest.mark.parametrize("variant", sorted(bench.VARIANTS))
def test_at_least_one_modality_is_on(variant):
    """Both off is refused at config-save time, so such a variant could never
    run — it would fail every document rather than measuring anything."""
    spec = bench.VARIANTS[variant]
    assert spec["text"] or spec["images"]


@pytest.mark.parametrize(
    "variant",
    sorted(v for v, s in bench.VARIANTS.items() if s["granularity"] == "packet"),
)
def test_a_packet_variant_gets_the_packet_prompt(docsplit_base, a_model, variant):
    """Packet granularity asks for a `segments` array; the page prompt asks for
    one class plus a boundary verdict and is read by a different parser, so a
    packet cell running the page prompt fails every document."""
    cfg = _config(docsplit_base, a_model, variant)
    packet_prompt = cfg["classification"]["packet"]["task_prompt"]
    # The keys the parser reads, not just the word "segments".
    for key in ("segments", "ordinal_start_page", "ordinal_end_page"):
        assert key in packet_prompt


@pytest.mark.parametrize(
    "variant",
    sorted(v for v, s in bench.VARIANTS.items() if s.get("strategy") == "two_stage"),
)
def test_a_two_stage_variant_gets_both_stage_prompts(docsplit_base, a_model, variant):
    cfg = _config(docsplit_base, a_model, variant)
    cls = cfg["classification"]
    granularity = bench.VARIANTS[variant]["granularity"]
    stage_a = (
        cls["segmentation"]["packet"]["task_prompt"]
        if granularity == "packet"
        else cls["segmentation"]["task_prompt"]
    )
    assert stage_a, "Stage A would fall back to the single-pass prompt"
    assert cls["labeling"]["task_prompt"]


def test_stage_a_carries_no_class_vocabulary(docsplit_base, a_model):
    """The prefix saving IS the point of two-stage. If a prompt change
    reintroduces the class list, two-stage still works and costs what
    single-pass cost — the one failure mode with no symptom."""
    cfg = _config(docsplit_base, a_model, "twostage-packet")
    stage_a = cfg["classification"]["segmentation"]["packet"]["task_prompt"]
    assert "{CLASS_NAMES_AND_DESCRIPTIONS}" not in stage_a
    assert "{FEW_SHOT_EXAMPLES}" not in stage_a


def test_no_variant_is_requested_without_a_model(docsplit_base, a_model):
    """An omitted variant keeps the pre-axis behaviour: the test set's own
    stored classification settings, with no granularity forced."""
    cfg = _config(docsplit_base, a_model, None)
    assert "modality" not in cfg["classification"] or cfg["classification"][
        "modality"
    ] == {"text": "", "images": ""}


# --------------------------------------------------------------------------
# The guard fires
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "break_it", "expected"),
    [
        ("packet-both", lambda c: c.update(granularity="page"), "granularity"),
        (
            "packet-both",
            lambda c: c["modality"].update(images=False),
            "modality.images",
        ),
        ("page-text", lambda c: c["modality"].update(text=False), "modality.text"),
        ("twostage-packet", lambda c: c.update(strategy="single_pass"), "strategy"),
        (
            # Mentions "segments" in prose but asks for no page ordinals, so the
            # parser cannot read what comes back. A substring test for the word
            # alone would pass this.
            "packet-both",
            lambda c: c["packet"].update(task_prompt="return the segments please"),
            "packet.task_prompt",
        ),
        (
            "packet-both",
            lambda c: c["packet"].update(task_prompt=""),
            "packet.task_prompt",
        ),
        (
            "twostage-packet",
            lambda c: c["segmentation"]["packet"].update(task_prompt=""),
            "Stage A",
        ),
        (
            "twostage-packet",
            lambda c: c["labeling"].update(task_prompt=""),
            "labeling.task_prompt",
        ),
        (
            "page-both",
            lambda c: c["confidence"].update(task_prompt_topk=""),
            "confidence.task_prompt_topk",
        ),
    ],
)
def test_the_guard_refuses_a_config_that_lost_its_setting(
    docsplit_base, a_model, variant, break_it, expected
):
    """⚠️ The direction that matters. A guard returning no problems for a broken
    config is worse than no guard, because the run then looks checked."""
    cfg = copy.deepcopy(_config(docsplit_base, a_model, variant))
    break_it(cfg["classification"])
    problems = bench.assert_variant_merged(cfg, variant, "topk")
    assert problems, f"breaking {expected} was not detected"
    assert any(expected in p for p in problems), (
        f"problems do not name {expected}: {problems}"
    )


def test_an_unknown_variant_raises_rather_than_passing():
    with pytest.raises(KeyError):
        bench.VARIANTS["no-such-variant"]


# --------------------------------------------------------------------------
# Cost attribution across the three metering contexts
# --------------------------------------------------------------------------


def test_all_three_classification_contexts_are_in_the_filter():
    assert set(bench.CLASSIFICATION_CONTEXTS) == {
        "Classification/",
        "Segmentation/",
        "Labeling/",
    }


def test_a_two_stage_run_is_not_reported_as_free():
    """⚠️ The defect this axis would otherwise have walked into. two_stage emits
    no `Classification/` entry at all, so a `Classification/`-only filter
    matched nothing and returned 0.0 — making the configuration with an extra
    call per segment look like the cheap one, which is the most misleading
    possible answer in a cost comparison."""
    metering = {
        "Segmentation/bedrock/us.amazon.nova-2-lite-v1:0": {
            "inputTokens": 1000,
            "outputTokens": 100,
        },
        "Segmentation/lambda/requests": {"requests": 1},
        "Labeling/bedrock/us.amazon.nova-2-lite-v1:0": {
            "inputTokens": 2000,
            "outputTokens": 200,
        },
        "Labeling/lambda/requests": {"requests": 3},
    }
    _cost, tokens, _why, by_context = bench.classification_cost(metering)
    assert tokens["inputTokens"] == 3000
    assert tokens["outputTokens"] == 300
    assert set(by_context) == {"Segmentation", "Labeling"}


def test_the_two_halves_are_reported_separately():
    """ "Which half is the money in?" is the question the strategy is chosen on,
    and a single total cannot answer it."""
    metering = {
        "Segmentation/bedrock/m": {"inputTokens": 10, "outputTokens": 1},
        "Labeling/bedrock/m": {"inputTokens": 90, "outputTokens": 9},
    }
    _cost, _tokens, _why, by_context = bench.classification_cost(metering)
    assert by_context["Segmentation"]["inputTokens"] == 10
    assert by_context["Labeling"]["inputTokens"] == 90


def test_the_per_stage_call_count_is_captured():
    """Packet granularity's N-calls-to-1 claim is a measurement, not an
    assertion, and this is the counter that makes it one."""
    metering = {
        "Classification/bedrock/m": {"inputTokens": 10},
        "Classification/lambda/requests": {"requests": 6},
    }
    _cost, _tokens, _why, by_context = bench.classification_cost(metering)
    assert by_context["Classification"]["calls"] == 6


def test_a_single_pass_run_still_reports_under_classification():
    """Back-compatibility of the figure: an existing comparison across releases
    must keep reading the same context with the same units."""
    metering = {
        "Classification/bedrock/m": {"inputTokens": 10, "outputTokens": 2},
        "OCR/textract/analyze_document-Tables": {"pages": 6},
    }
    _cost, tokens, _why, by_context = bench.classification_cost(metering)
    assert set(by_context) == {"Classification"}
    # The OCR entry is another step's cost and must not leak in.
    assert tokens["inputTokens"] == 10


def test_empty_metering_is_not_an_error():
    cost, tokens, _why, by_context = bench.classification_cost({})
    assert tokens == {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0}
    assert by_context == {}
    assert cost == 0 or cost is None


# --------------------------------------------------------------------------
# The boundary / label split the run is read on
# --------------------------------------------------------------------------


def test_score_classification_surfaces_both_halves():
    analyze = harness_module("analyze")
    scored = analyze.score_classification(
        {
            "doc_split_metrics": {
                "page_level_accuracy": 0.9,
                "boundary_accuracy": 1.0,
                "label_accuracy": 0.5,
                "split_accuracy_without_order": 0.5,
                "page_details": [],
            }
        }
    )
    assert scored["boundary_accuracy"] == 1.0
    assert scored["label_accuracy"] == 0.5
    # The decomposition is exact, which is what makes the pair readable.
    assert scored["boundary_accuracy"] * scored["label_accuracy"] == pytest.approx(
        scored["split_accuracy_without_order"]
    )


def test_an_unscored_label_half_stays_none_rather_than_zero():
    """⚠️ Null means no section's boundaries matched, so no label was judged.
    Reporting 0.0 would say "every label was wrong", which is a different fact
    and would drag a pooled average down."""
    analyze = harness_module("analyze")
    scored = analyze.score_classification(
        {
            "doc_split_metrics": {
                "page_level_accuracy": 0.5,
                "boundary_accuracy": 0.0,
                "label_accuracy": None,
                "page_details": [],
            }
        }
    )
    assert scored["boundary_accuracy"] == 0.0
    assert scored["label_accuracy"] is None


def test_an_older_report_without_the_halves_reports_none():
    """Results committed before #1510 carry neither key."""
    analyze = harness_module("analyze")
    scored = analyze.score_classification(
        {"doc_split_metrics": {"page_level_accuracy": 0.9, "page_details": []}}
    )
    assert scored["boundary_accuracy"] is None
    assert scored["label_accuracy"] is None
    assert scored["class_accuracy"] == 0.9
