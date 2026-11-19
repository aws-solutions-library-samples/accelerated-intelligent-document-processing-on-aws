# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The "Which approach should I use?" section (render_splitting_approaches.py).

It is regenerated while runs are still going, so most of these pin what it does with a
value that is not measured yet: the clause goes, the sentence goes if nothing is left,
and the still-running line names it. The rest pin the answer's rule (a winner only by
more than twice the combined standard error) and the FAQ entry render_guidance.py
leaves in place of a `feature` ablation.
"""

from __future__ import annotations

import copy
import json
import re

import pytest
from harness_import import harness_module

rsa = harness_module("render_splitting_approaches")
rg = harness_module("render_guidance")

ROWS = [
    "Page by page",
    "Holistic",
    "Split, then an LLM names each section",
    "Split, then a decision model names each section",
]
MODELS = ["Amazon Nova 2 Lite", "Claude Haiku 5.5", "Claude Sonnet 5.5"]
COST = "classification_cost_per_1000_docs"
ACC = "section_accuracy"


def registry_entry() -> dict:
    return {
        "id": "SC-13",
        "page": rsa.PAGE,
        "question": "Which way of splitting and classifying should I use?",
        "feature": rsa.HEADING,
        "changes": "How the packet is split",
        "values": [f"{r} · {m}" for m in MODELS for r in ROWS],
        "grid": {"rows": ROWS, "cols": MODELS},
        "datasets": ["docsplit_100", "docsplit_sameclass"],
        "metrics": [ACC],
        "cost_metric": COST,
        "runs": 3,
    }


def registry() -> dict:
    return {
        "datasets": {
            "docsplit_100": {
                "name": "DocSplit",
                "chart": "DocSplit packets",
                "kind": "packet",
            },
            "docsplit_sameclass": {
                "name": "Same",
                "chart": "Same-class",
                "kind": "packet",
            },
        },
        "metrics": {
            ACC: {
                "name": "Section accuracy",
                "axis": "Sections exactly right (%)",
                "kinds": ["packet"],
            },
            COST: {
                "name": "Classification cost per 1,000 documents (USD)",
                "kinds": ["packet"],
            },
        },
        "ablations": [registry_entry()],
    }


#: (row, model): (section accuracy, its SE, cost)
GRID = {
    (0, 0): (0.49, 0.007, 13.2),
    (1, 0): (0.51, 0.013, 5.4),
    (2, 0): (0.46, 0.010, 7.8),
    (0, 1): (0.70, 0.002, 14.5),
    (1, 1): (0.75, 0.019, 10.4),
    (2, 1): (0.75, 0.005, 16.4),
    (0, 2): (0.73, 0.002, 252.0),
    (1, 2): (0.90, 0.002, 200.0),
    (2, 2): (0.88, 0.006, 315.0),
}


def results(grid: dict = GRID, ds: str = "docsplit_100") -> dict:
    cells = {}
    for (r, m), (acc, se, cost) in grid.items():
        value = f"{ROWS[r]} · {MODELS[m]}"
        cells[f"{ds}|{value}|{ACC}"] = {"mean": acc, "se": se}
        cells[f"{ds}|{value}|{COST}"] = {"mean": cost, "se": 0.05}
    return {"region": "us-east-1", "cells": cells, "sample": {}}


def data(res: dict | None = None, analyses: dict | None = None) -> object:
    reg = registry()
    return rsa.Data(
        reg, reg["ablations"][0], res or results(), analyses or {}, None, None
    )


def test_the_cost_axis_splits_only_at_a_wide_gap():
    assert rsa.cost_split([5.4, 10.4, 16.4, 200.0, 315.0]) == 200.0
    assert rsa.cost_split([5.4, 10.4, 16.4]) is None


def test_the_start_is_the_cheapest_point_tied_with_the_best_of_the_cheap_tier():
    pk = rsa.picks(data().points("docsplit_100"))
    # Holistic and split tie on Claude Haiku 5.5 (0.75, the SEs overlap); holistic is cheaper.
    assert (pk.start.approach.row, pk.start.model) == ("Holistic", "Claude Haiku 5.5")
    assert pk.upper == 200.0 and pk.unbeaten
    # Holistic Claude Sonnet 5.5 beats split by more than twice the combined SE.
    assert (pk.best.approach.row, pk.best.model) == ("Holistic", "Claude Sonnet 5.5")


def test_no_winner_is_named_inside_twice_the_combined_standard_error():
    grid = dict(GRID)
    grid[(2, 2)] = (0.895, 0.006, 315.0)  # within 2·SE of holistic's 0.90
    pk = rsa.picks(data(results(grid)).points("docsplit_100"))
    assert pk.best is None
    assert len(pk.top) == 2
    text = rsa.answer(data(results(grid)))
    assert "holistic and split then an LLM got the most right" in text


def test_the_answer_quotes_the_numbers_it_rests_on():
    text = rsa.answer(data())
    assert text.startswith(
        "Holistic classification with Claude Haiku 5.5 is a good place to start"
    )
    assert "75% of sections exactly right for $10.4" in text
    assert "no approach under $200 got measurably more right" in text
    assert "With Claude Sonnet 5.5 it got the most right, 90%, for $200" in text


def test_a_value_still_running_is_named_once_and_leaves_no_blank():
    d = data()
    line = rsa.missing_line(d)
    # Nothing measured for the decision model, nor for same-class packets.
    assert (
        "split then a decision model with Amazon Nova 2 Lite, Claude Haiku 5.5 and Claude Sonnet 5.5"
        in line
    )
    pk = rsa.picks(d.points("docsplit_100"))
    sub = "\n".join(rsa.subsection(d, d.approaches[3], pk))
    assert "*Still running.*" in sub
    for a in d.approaches[:3]:
        text = "\n".join(rsa.subsection(d, a, pk))
        assert not re.search(r"\bNone\b|\bnan\b|\$ |  ", text), text


def test_a_partly_measured_approach_says_which_models_are_still_running():
    grid = dict(GRID)
    grid[(3, 0)] = (0.45, 0.004, 9.5)
    d = data(results(grid))
    sub = "\n".join(
        rsa.subsection(d, d.approaches[3], rsa.picks(d.points("docsplit_100")))
    )
    assert (
        "Classification costs $9.50 with Amazon Nova 2 Lite per 1,000 packets." in sub
    )
    assert "With Claude Haiku 5.5 and Claude Sonnet 5.5: still running." in sub


def test_a_dataset_gets_a_panel_only_once_enough_is_measured():
    res = results()
    few = {
        k: v
        for k, v in results(ds="docsplit_sameclass")["cells"].items()
        if "Nova" in k and "Page" in k
    }
    res["cells"].update(few)
    assert data(res).panels() == ["docsplit_100"]
    res["cells"].update(results(ds="docsplit_sameclass")["cells"])
    assert data(res).panels() == ["docsplit_100", "docsplit_sameclass"]


def test_a_sentence_quoting_an_analysis_is_left_out_without_one():
    d = data()
    pk = rsa.picks(d.points("docsplit_100"))
    page = "\n".join(rsa.subsection(d, d.approaches[0], pk))
    assert "predicted" not in page and "merged" not in page
    analysis = {
        "packets": {
            "n": 80,
            "analysed_ground_truth": {"packets": 80, "true_sections": 316},
        },
        "values": {
            "Page by page · Claude Haiku 5.5": {
                "runs": ["a", "b", "c"],
                "errors": {
                    "shares": {
                        k: {"mean": v}
                        for k, v in {
                            "correct": 0.7,
                            "wrong_class": 0.02,
                            "merged": 0.0,
                            "split": 0.28,
                            "other": 0.0,
                        }.items()
                    }
                },
                "predicted_sections_per_packet": {"mean": 6.39},
            }
        },
    }
    d = data(analyses={"docsplit_100": analysis})
    page = "\n".join(rsa.subsection(d, d.approaches[0], pk))
    assert "It never merged two documents into one." in page
    assert (
        "predicted 6.4 sections per packet against 3.95 true, so 28% of sections were split"
        in page
    )


def test_the_region_goes_in_place_of_the_explainer_and_then_between_its_markers(
    tmp_path,
):
    page = f"Intro.\n\n![x]({rsa.EXPLAINER})\n\n## Next\n"
    once = rsa.splice(page, "## A\n", tmp_path)
    begin, end = rg.markers(rsa.REGION)
    assert once == f"Intro.\n\n{begin}\n\n## A\n\n{end}\n\n## Next\n"
    assert rsa.splice(once, "## B\n", tmp_path) == once.replace("## A", "## B")
    with pytest.raises(SystemExit):
        rsa.splice("no figure here\n", "## A\n", tmp_path)


def test_a_feature_ablation_keeps_its_question_and_links_to_its_section(monkeypatch):
    entry = registry_entry()
    out = rg.render_entry(entry, registry(), results())
    assert out.faq == [
        f"### {entry['question']}",
        "",
        "See [Which approach should I use?](#which-approach-should-i-use).",
        "",
    ]
    assert out.plots == {}
    seen = []
    monkeypatch.setattr(
        rg, "render_ablation", lambda ab, reg, res: seen.append(ab["id"]) or "drawn"
    )
    plain = {k: v for k, v in copy.deepcopy(entry).items() if k != "feature"}
    assert rg.render_entry(plain, registry(), results()) == "drawn" and seen == [
        "SC-13"
    ]


def test_a_feature_must_name_its_heading():
    with pytest.raises(SystemExit):
        rg.render_feature({**registry_entry(), "feature": True})


def test_the_approach_hues_are_the_first_three_validated_slots():
    # The dataviz palette's slots 1-3, light and dark: the only three that pass every
    # check on all pairs, as a scatter needs. The fourth approach shares the third
    # hue and differs by shape.
    assert rsa.HUES == {
        "page": ("#2a78d6", "#3987e5"),
        "holistic": ("#eb6834", "#d95926"),
        "split": ("#1baf7a", "#199e70"),
    }
    shapes = [shape for *_, shape in rsa.APPROACH_NAMES]
    assert len(set(shapes)) == len(shapes)


def paired_points(scored: bool) -> list[dict]:
    """140 packets shaped like SC-14's: on 5-20 pages holistic leads (about +0.08),
    on 30-60 pages the two are level and both lower."""
    out = []
    for i in range(100):
        a, b = 0.76 + 0.2 * ((i % 5) - 2) / 2, 0.68 + 0.2 * ((i % 7) - 3) / 3
        out.append(
            {
                "pages": 5 + i % 16,
                "dataset": "docsplit_100",
                "delta": a - b,
                "a_score": a,
                "b_score": b,
            }
        )
    for i in range(40):
        a, b = 0.62 + 0.2 * ((i % 5) - 2) / 2, 0.62 + 0.2 * ((i % 4) - 1.5) / 1.5
        out.append(
            {
                "pages": 30 + i % 31,
                "dataset": "docsplit_long",
                "delta": a - b,
                "a_score": a,
                "b_score": b,
            }
        )
    if not scored:
        out = [{k: v for k, v in p.items() if not k.endswith("_score")} for p in out]
    return out


def long_data(sc14_cells: dict | None, scored: bool = True) -> object:
    reg = registry()
    reg["datasets"]["docsplit_long"] = {
        "name": "DocSplit long packets (40 packets, 30 to 60 pages)",
        "kind": "packet",
    }
    long_ab = {
        "id": "SC-14",
        "datasets": ["docsplit_100", "docsplit_long"],
        "values": [
            "Page by page · Claude Haiku 5.5",
            "Holistic · Claude Haiku 5.5",
            "Split, then an LLM classifies each section · Claude Haiku 5.5",
        ],
    }
    be = {
        "a": "Holistic · Claude Haiku 5.5",
        "b": "Page by page · Claude Haiku 5.5",
        "line": {"crossing_pages": 52.6, "crossing_ci95": [33, 221]},
        "points": paired_points(scored),
    }
    sc14 = {"cells": sc14_cells} if sc14_cells is not None else None
    return rsa.Data(reg, reg["ablations"][0], results(), {}, long_ab, sc14, be)


LONG = "docsplit_long|{}|{}"
SHORT = "docsplit_100|{}|{}"
HOL, PAGE = "Holistic · Claude Haiku 5.5", "Page by page · Claude Haiku 5.5"
CELLS = {
    SHORT.format(HOL, ACC): {"mean": 0.746, "se": 0.019},
    SHORT.format(PAGE, ACC): {"mean": 0.696, "se": 0.002},
    LONG.format(HOL, ACC): {"mean": 0.619, "se": 0.013},
    LONG.format(PAGE, ACC): {"mean": 0.619, "se": 0.004},
    LONG.format(HOL, COST): {"mean": 35.18, "se": 0.25},
    LONG.format(PAGE, COST): {"mean": 44.62, "se": 0.07},
}


def test_percentiles_interpolate_between_packets():
    assert rsa.quantile([0, 10], 0.5) == 5
    assert rsa.quantile([1, 2, 3, 4, 5], 0.25) == 2
    assert rsa.signed(-0.001) == "0.00" and rsa.signed(0.078) == "+0.08"


def repo_long_data() -> object:
    """The synthetic registry with SC-14's own paired analysis (the repo's file)."""
    path = rg.RESULTS / "SC-14-breakeven.json"
    be = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if "metrics" not in be:
        pytest.skip("SC-14-breakeven.json has no per-metric pairs")
    d = long_data(dict(CELLS), scored=False)
    d.breakeven = be
    return d


def test_the_long_packet_takeaway_claims_no_more_than_twice_the_standard_error():
    # SC-14: sections exactly right +0.078 ± 0.047 short, -0.001 ± 0.060 long;
    # grouping score +0.062 ± 0.024 long; label accuracy -0.048 ± 0.025 long.
    d = repo_long_data()
    answer, why, split = rsa.long_text(d, rsa.long_facts(d))
    assert answer.startswith(
        "Holistic stays a good place to start on long packets: there its advantage is "
        "in grouping pages, not in getting every boundary exactly right."
    )
    assert "about a fifth less than page by page ($35.2 against $44.6" in answer
    assert (
        "On that strict measure, holistic's lead on short packets (+0.08 ± 0.05) isn't "
        "measurable on long ones (0.00 ± 0.06)." in why
    )
    assert (
        "But on the grouping score, which gives credit for nearly right sections, "
        "holistic still leads clearly on long packets (+0.06 ± 0.02), as it does on "
        "V-measure, Rand index and ordering." in why
    )
    assert "Page accuracy is the same, and page by page labels the sections" in why
    assert split.startswith(
        "Splitting first, then having an LLM classify each section, got no measurably "
        "more sections exactly right than holistic on long packets"
    )
    text = " ".join([answer, why, split])
    assert "52" not in text and "gone" not in text and "break" not in text


def test_the_metric_comparison_names_every_metric_and_keeps_its_title_short():
    d = repo_long_data()
    svg, alt = rsa.forest_chart(d, rsa.long_facts(d))
    title = re.search(r"<title>([^<]*)</title>", svg).group(1)
    assert len(title.split()) <= 14
    assert (
        title
        == "With Claude Haiku 5.5, holistic still groups long packets better than page by page"
    )
    for _, name, _ in rsa.SPLIT_METRICS:
        assert f">{name}</text>" in svg
    assert "holistic better →" in svg and "No difference" in svg
    defs = "\n".join(rsa.metric_definitions())
    assert "V-measure" in defs and "Rand index" in defs and "Ordering" in defs
    assert "(#questions)" in defs and rsa.STICKLER in defs


def test_a_clear_change_is_said_as_one():
    d = long_data(dict(CELLS))
    _, why, *_ = rsa.long_text(d, rsa.long_facts(d))
    assert why.startswith(
        "Long packets are harder for both approaches: on 30–60 pages both got 62% of "
        "sections exactly right, against 76% for holistic and 68% for page by page"
    )


def test_harder_is_said_only_for_a_drop_beyond_its_packet_to_packet_spread():
    f = rsa.long_facts(long_data(dict(CELLS)))
    assert f.per_packet
    clear = [name for name, s, lg in f.absolute if rsa.gap_clear(s, lg)]
    words = rsa.harder_words(f)
    if len(clear) == 2:
        assert words == "Long packets are harder for both approaches"
    elif clear:
        assert words.startswith("Long packets are harder, clearly so for")
    # From SC-14's cells (run-to-run noise only) the claim is never made.
    f = rsa.long_facts(long_data(dict(CELLS), scored=False))
    assert not f.per_packet and rsa.harder_words(f) == ""
    paras = rsa.long_text(long_data(dict(CELLS), scored=False), f)
    assert any(
        p.startswith(
            "On 30–60 pages both got 62% of sections exactly right, against 75%"
        )
        for p in paras
    )


def test_the_long_packet_views_and_what_is_still_running():
    d = long_data(None)
    lines, charts = rsa.long_section(d)
    assert list(charts) == [rsa.LONG_SVG]
    svg = charts[rsa.LONG_SVG]
    assert "better 50" not in svg or "tie" in svg
    assert svg.count('class="median"') == 6  # four boxes on the left, two on the right
    assert "5th to 95th percentile" in svg and "No difference" in svg
    assert "Split, then an LLM" not in "\n".join(lines)
    assert "split then an LLM on long packets" in rsa.missing_line(d)
    assert "holistic and" not in rsa.missing_line(d)
    # Without each packet's own scores the left view keeps its dots and drops its boxes.
    _, charts = rsa.long_section(long_data(dict(CELLS), scored=False))
    assert charts[rsa.LONG_SVG].count('class="median"') == 2


def test_split_then_classify_is_judged_only_from_its_paired_figures():
    # Its long-packet cell alone (0.66 against holistic's 0.62) says nothing paired.
    cells = dict(CELLS)
    split = "Split, then an LLM classifies each section · Claude Haiku 5.5"
    cells[LONG.format(split, ACC)] = {"mean": 0.66, "se": 0.01}
    d = long_data(cells)
    assert not any("Splitting first" in p for p in rsa.long_text(d, rsa.long_facts(d)))
