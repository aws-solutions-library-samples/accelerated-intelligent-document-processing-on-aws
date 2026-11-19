#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Render the "Which approach should I use?" section of the splitting benchmarks page.

docs/benchmark-guidance/splitting-and-classification.md opens with the question readers
ask most. Its answer is generated into the region between

    <!-- BEGIN GENERATED APPROACHES --> and <!-- END GENERATED APPROACHES -->

right after the page's intro, from the ablation the registry marks `feature` (SC-13:
four approaches, each with three models). render_guidance.py leaves that ablation's FAQ
entry as one line linking here, and never touches this region.

The section, in order: the explainer figure; the answer, which names a good place to
start (the most accurate value of the cheaper cost tier, or the cheapest one no other
value there beats by more than twice the combined standard error) and the most
accurate value overall when it wins by that margin; the economics render_guidance.py
writes for the ablation; a chart of sections exactly right against classification
cost, one point per approach and model, colored by approach, a panel per dataset; a
chart of where each approach loses sections; one subsection per approach; long packets
(SC-14) once measured; and the full tables, collapsed.

What it reads:

    benchmarks/results/guidance/SC-13.json      section accuracy and classification
                                                cost per dataset, approach and model
    .../SC-13-analysis-<dataset>.json           the per-packet breakdown that
                                                analyze_splitting_approaches.py writes:
                                                error kinds, sections per packet,
                                                confidence
    .../SC-14.json                              the same approaches on long packets

Every sentence is built from the cells it quotes: a cell not measured yet (a run still
going) drops its clause, a sentence left with nothing to say is left out, and one
"Still running" line lists what is missing. A dataset panel is drawn once it has
MIN_PANEL_POINTS points over at least two approaches.

The charts go to images/benchmark-guidance/approaches/, a directory of their own: the
full render_guidance.py run deletes the top-level charts it did not draw, and only
those. Colors: three categorical hues, the first three slots of the validated default
palette, which pass every check for all pairs (a scatter puts any two points side by
side) in light and dark mode; no four slots do. The two split approaches share the
third hue, as they share the section-detection step, and every approach has its own
marker shape, so no approach is told by color alone.

Usage:
    python3 benchmarks/harness/render_splitting_approaches.py            # rewrite the region
    python3 benchmarks/harness/render_splitting_approaches.py --check    # exit 1 if stale
    python3 benchmarks/harness/render_splitting_approaches.py --out DIR  # write a copy:
        DIR/docs/benchmark-guidance/<page>.md, DIR/images/.../approaches/*.svg and
        DIR/approaches.md (the region alone); the real page is not touched
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from guidance_plots import STYLE, marker, money, nice_ticks, pct
from render_guidance import (
    EMPTY,
    PAGES,
    PLOTS,
    PLOTS_FROM_PAGES,
    REPO,
    RESULTS,
    axis_title,
    cell_text,
    chart_name,
    clear_gap,
    cost_axis_title,
    cost_column,
    fix_id_links,
    load_registry,
    markers,
    n_short,
    price_caveat_svg,
    render_ablation,
    render_econ,
    sample_sizes,
    winner,
)

PAGE = "splitting-and-classification"
REGION = "APPROACHES"
HEADING = "Which approach should I use?"
CHARTS = PLOTS / "approaches"
CHARTS_FROM_PAGES = f"{PLOTS_FROM_PAGES}/approaches"
EXPLAINER = f"{PLOTS_FROM_PAGES}/figures/splitting-approaches.svg"
EXPLAINER_ALT = (
    "Four ways to find the documents in a packet and classify each one: page by "
    "page, holistic, split then an LLM classifies each section, and split then a "
    "decision model classifies each section"
)
HEADLINE_SVG = "sections-against-cost.svg"
LOSSES_SVG = "where-sections-are-lost.svg"
#: The long-packet ablation: the same approaches on 30-to-60-page packets.
LONG_ID = "SC-14"
#: Holistic minus page by page per packet against its length, from
#: fit_length_breakeven.py: the per-band paired differences the long-packet chart draws.
BREAKEVEN = f"{LONG_ID}-breakeven.json"
LONG_SVG = "long-packets.svg"
METRICS_SVG = "long-packets-metrics.svg"
ACCURACY = "section_accuracy"
#: A dataset gets its own panel once this many approach-and-model points are measured.
MIN_PANEL_POINTS = 4
#: Panel names, where the registry's chart name would not say what the packets hold.
PANEL_NAMES = {"docsplit_sameclass": "Same class back to back"}
#: How the sentences name a dataset.
DATASET_WORDS = {
    "docsplit_100": "DocSplit packets",
    "docsplit_sameclass": "same-class packets",
}
#: Costs this many times apart, with nothing between, split the cost axis in two.
TIER_RATIO = 4.0
#: The issue about holistic replies that leave out the section confidence.
NO_CONFIDENCE_ISSUE = (
    "[#2039](https://github.com/aws-solutions-library-samples/"
    "accelerated-intelligent-document-processing-on-aws/issues/2039)"
)
DECISION_MODEL = "Strands Decider 2B"


@dataclass(frozen=True)
class Approach:
    """One of the four approaches: the grid row the results name it by, how this
    section names it, and its mark (hue class and marker shape)."""

    row: str
    name: str
    legend: str
    brief: str
    hue: str
    shape: str

    @property
    def lower(self) -> str:
        return self.name[:1].lower() + self.name[1:]


#: The approaches in the registry's grid order, as readers see them. The results name
#: the split rows after what the second step does to a section; the page calls that
#: section classification.
APPROACH_NAMES = (
    ("Page by page", "Page by page", "page by page", "page", "circle"),
    ("Holistic", "Holistic", "holistic", "holistic", "square"),
    (
        "Split, then an LLM classifies each section",
        "Split, then an LLM classifies",
        "split then an LLM",
        "split",
        "triangle",
    ),
    (
        "Split, then a decision model classifies each section",
        "Split, then a decision model classifies",
        "split then a decision model",
        "split",
        "diamond",
    ),
)
#: The approach hues, light and dark: slots 1-3 of the dataviz skill's default palette.
HUES = {
    "page": ("#2a78d6", "#3987e5"),
    "holistic": ("#eb6834", "#d95926"),
    "split": ("#1baf7a", "#199e70"),
}
#: The error kinds analyze_splitting_approaches.py counts, in the order the loss chart
#: shows them, as readers see them.
LOSS_KINDS = (
    ("wrong_class", "Wrong class"),
    ("merged", "Merged with a neighbour"),
    ("split", "Split into pieces"),
    ("other", "Pages left out"),
)
#: An error kind no row reaches this share of gets a line under the chart, not a column.
LOSS_FLOOR = 0.02


def approaches(ab: dict[str, Any]) -> list[Approach]:
    rows = list((ab.get("grid") or {}).get("rows") or [])
    if len(rows) != len(APPROACH_NAMES):
        raise SystemExit(
            f"{ab['id']}: expected {len(APPROACH_NAMES)} grid rows, got {len(rows)}"
        )
    return [
        Approach(row, *names) for row, names in zip(rows, APPROACH_NAMES, strict=True)
    ]


def join(items: Sequence[str], word: str = "and") -> str:
    """ "a", "a and b", "a, b and c"."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {word} {items[-1]}"


def share(v: float) -> str:
    """A share of sections in words: 28%, "under 1%" or "none"."""
    if v <= 0:
        return "none"
    return "under 1%" if v < 0.005 else pct(v)


def times_text(ratio: float) -> str:
    return f"{ratio:.0f} times" if ratio >= 10 else f"{ratio:.1f} times"


# ---------------------------------------------------------------------------------
# The measurements


@dataclass
class Point:
    """One approach and model on one dataset: its cost and section accuracy cells."""

    approach: Approach
    model: str
    x: dict[str, float]
    y: dict[str, float]

    @property
    def value(self) -> str:
        return f"{self.approach.row} · {self.model}"


class Data:
    """SC-13's results, its per-packet analyses and SC-14's results, as the section
    reads them. Every accessor returns None for what is not measured."""

    def __init__(
        self,
        reg: dict[str, Any],
        ab: dict[str, Any],
        results: dict[str, Any] | None,
        analyses: dict[str, dict[str, Any]],
        long_ab: dict[str, Any] | None,
        long_results: dict[str, Any] | None,
        breakeven: dict[str, Any] | None = None,
    ) -> None:
        self.reg, self.ab = reg, ab
        self.breakeven = breakeven or {}
        self.results = results or {}
        self.cells: dict[str, Any] = dict(self.results.get("cells") or {})
        self.analyses = analyses
        self.long_ab, self.long_results = long_ab, long_results or {}
        self.approaches = approaches(ab)
        self.models: list[str] = list(ab["grid"]["cols"])
        self.datasets: list[str] = list(ab["datasets"])
        kind = reg["datasets"][self.datasets[0]]["kind"]
        self.cost = cost_column(ab, kind, reg["metrics"])

    def cell(
        self, ds: str, a: Approach, model: str, metric: str
    ) -> dict[str, float] | None:
        c = self.cells.get(f"{ds}|{a.row} · {model}|{metric}")
        return c if isinstance(c, dict) and "mean" in c else None

    def points(self, ds: str) -> list[Point]:
        out = []
        for a in self.approaches:
            for m in self.models:
                x, y = self.cell(ds, a, m, self.cost), self.cell(ds, a, m, ACCURACY)
                if x and y:
                    out.append(Point(a, m, x, y))
        return out

    def panels(self) -> list[str]:
        """The datasets measured well enough for a panel of their own."""
        out = []
        for ds in self.datasets:
            pts = self.points(ds)
            if len(pts) >= MIN_PANEL_POINTS and len({p.approach for p in pts}) >= 2:
                out.append(ds)
        return out

    def analysed(self, ds: str, a: Approach, model: str) -> dict[str, Any] | None:
        return ((self.analyses.get(ds) or {}).get("values") or {}).get(
            f"{a.row} · {model}"
        )

    def loss(self, ds: str, a: Approach, model: str, kind: str) -> float | None:
        v = self.analysed(ds, a, model)
        cell = (((v or {}).get("errors") or {}).get("shares") or {}).get(kind)
        return float(cell["mean"]) if isinstance(cell, dict) else None

    def auroc(
        self, ds: str, a: Approach, model: str, quoted: bool = False
    ) -> dict[str, float] | None:
        """A confidence AUROC over two runs or more, or None: one run's is too few
        wrong sections to compare. `quoted`: also None where nearly every section has
        the same confidence, so the AUROC rests on the few that differ; such a figure
        still counts when asking whether another approach beats it."""
        conf = (self.analysed(ds, a, model) or {}).get("confidence") or {}
        cell = conf.get("auroc")
        if conf.get("constant") or not isinstance(cell, dict):
            return None
        if quoted and any(
            str(n).startswith("nearly constant") for n in conf.get("notes") or []
        ):
            return None
        return cell if int(cell.get("runs") or 0) >= 2 else None

    def without_confidence(self, ds: str, a: Approach, model: str) -> tuple[int, int]:
        """Sections with matched pages that carried no confidence, and all of them."""
        conf = (self.analysed(ds, a, model) or {}).get("confidence") or {}
        none = int(conf.get("n_without_confidence") or 0)
        total = none + int(conf.get("n_right") or 0) + int(conf.get("n_wrong") or 0)
        return none, total

    def predicted_sections(self, ds: str, a: Approach, model: str) -> float | None:
        cell = (self.analysed(ds, a, model) or {}).get("predicted_sections_per_packet")
        return float(cell["mean"]) if isinstance(cell, dict) else None

    def true_sections(self, ds: str) -> float | None:
        truth = ((self.analyses.get(ds) or {}).get("packets") or {}).get(
            "analysed_ground_truth"
        ) or {}
        if not truth.get("packets"):
            return None
        return truth["true_sections"] / truth["packets"]

    def analysed_n(self, ds: str) -> str:
        """ "80 packets × 2–3 runs": the packets every run of the analysis scored."""
        an = self.analyses.get(ds) or {}
        n = (an.get("packets") or {}).get("n")
        runs = sorted(
            {
                len(v.get("runs") or [])
                for v in (an.get("values") or {}).values()
                if v.get("runs")
            }
        )
        if not n or not runs:
            return ""
        span = f"{runs[0]}" if runs[0] == runs[-1] else f"{runs[0]}–{runs[-1]}"
        return f"{n} packets × {span} runs"

    def sample(self, ds: str, values: list[str]) -> str:
        sizes = sample_sizes(self.results.get("sample") or {}, [ds], values)
        return n_short(sizes[ds], "packets") if ds in sizes else ""

    def missing(self, ds: str) -> list[tuple[Approach, str]]:
        return [
            (a, m)
            for a in self.approaches
            for m in self.models
            if not (self.cell(ds, a, m, ACCURACY) and self.cell(ds, a, m, self.cost))
        ]


# ---------------------------------------------------------------------------------
# What the numbers say


def gap_clear(a: dict[str, float], b: dict[str, float]) -> bool:
    """Whether `a` beats `b` by more than twice the combined standard error."""
    return a["mean"] - b["mean"] > clear_gap(a, b)


def cost_split(costs: list[float]) -> float | None:
    """The lowest cost of the upper tier, when the costs fall into two tiers TIER_RATIO
    or more apart with nothing between; else None."""
    xs = sorted(c for c in costs if c > 0)
    if len(xs) < 2:
        return None
    ratio, at = max((xs[i + 1] / xs[i], i + 1) for i in range(len(xs) - 1))
    return xs[at] if ratio >= TIER_RATIO else None


@dataclass
class Picks:
    """The answer's two points: where to start, and the most accurate overall."""

    start: Point
    #: The lowest cost of the dearer tier, when there is one.
    upper: float | None
    #: Whether no point cheaper than `upper` beats `start` by a clear gap.
    unbeaten: bool
    #: The most accurate point, when it beats every other by a clear gap.
    best: Point | None
    #: The points no other beats by a clear gap: `best` alone when there is one.
    top: list[Point]


def picks(points: list[Point]) -> Picks | None:
    if not points:
        return None
    upper = cost_split([p.x["mean"] for p in points])
    lower = [p for p in points if upper is None or p.x["mean"] < upper]
    top = max(lower, key=lambda p: p.y["mean"])
    tied = [p for p in lower if not gap_clear(top.y, p.y)]
    start = min(tied, key=lambda p: p.x["mean"])
    unbeaten = not any(gap_clear(p.y, start.y) for p in lower)
    best_key = winner({p.value: p.y for p in points}, ACCURACY)
    best = next((p for p in points if p.value == best_key), None)
    most = max(points, key=lambda p: p.y["mean"])
    top = [best] if best else [p for p in points if not gap_clear(most.y, p.y)]
    return Picks(start, upper, unbeaten, best, sorted(top, key=lambda p: -p.y["mean"]))


def model_matters_more(points: list[Point]) -> bool:
    """Whether every approach's spread across models (each a clear gap) is wider than
    any model's spread across approaches."""
    by_approach: dict[Approach, list[Point]] = {}
    by_model: dict[str, list[Point]] = {}
    for p in points:
        by_approach.setdefault(p.approach, []).append(p)
        by_model.setdefault(p.model, []).append(p)
    across_models = []
    for pts in by_approach.values():
        if len(pts) < 2:
            continue
        hi = max(pts, key=lambda p: p.y["mean"])
        lo = min(pts, key=lambda p: p.y["mean"])
        if not gap_clear(hi.y, lo.y):
            return False
        across_models.append(hi.y["mean"] - lo.y["mean"])
    across_approaches = [
        max(p.y["mean"] for p in pts) - min(p.y["mean"] for p in pts)
        for pts in by_model.values()
        if len(pts) >= 2
    ]
    if len(across_models) < 2 or len(across_approaches) < 2:
        return False
    return min(across_models) > max(across_approaches)


def approach_phrase(p: Point) -> str:
    """ "Holistic classification with Claude Haiku 5.5"."""
    a = p.approach
    if a.hue in ("page", "holistic"):
        return f"{a.name} classification with {p.model}"
    return f"{a.name}, with {p.model} finding the sections,"


def answer(data: Data) -> str:
    ds = data.datasets[0]
    pk = picks(data.points(ds))
    if pk is None:
        return "Not yet measured."
    s, words = pk.start, DATASET_WORDS.get(ds, chart_name(ds, data.reg["datasets"]))
    first = (
        f"{approach_phrase(s)} is a good place to start. On {words} it got "
        f"{pct(s.y['mean'])} of sections exactly right for {money(s.x['mean'])} of "
        "classification per 1,000 packets"
    )
    if pk.unbeaten and pk.upper is not None:
        first += f"; no approach under {money(pk.upper)} got measurably more right"
    out = [first + "."]
    top = pk.top
    if top and s not in top:
        # The trade-off: what the most accurate point, or points tied for it, cost.
        cheapest = min(p.x["mean"] for p in top)
        ratio = cheapest / s.x["mean"] if s.x["mean"] else 0
        accs = join([pct(p.y["mean"]) for p in top])
        cost = join([money(p.x["mean"]) for p in top])
        if ratio >= 2:
            cost += (
                f", {times_text(ratio)} as much"
                if len(top) == 1
                else (f", at least {times_text(ratio)} as much")
            )
        models = {p.model for p in top}
        if len(top) == 1 and top[0].approach == s.approach:
            out.append(
                f"With {top[0].model} it got the most right, {accs}, for {cost}."
            )
        elif len(models) == 1:
            names = join([p.approach.brief for p in top])
            out.append(
                f"With {top[0].model}, {names} got the most right, {accs}, for {cost}."
            )
        else:
            names = join([f"{p.approach.brief} with {p.model}" for p in top])
            out.append(
                f"{names[:1].upper() + names[1:]} got the most right, {accs}, for {cost}."
            )
    out.append(
        "Reading text only, without page images, does better still "
        "([SC-02](#do-i-need-page-images-in-the-classification-prompt))."
    )
    out.append("Test it on your own packets.")
    return " ".join(out)


def missing_line(data: Data) -> str:
    """One line on what is still running, or "": each setting missing on the first
    dataset; on the others, the models missing altogether and a count of the rest."""
    parts = []
    for i, ds in enumerate(data.datasets):
        miss = data.missing(ds)
        if not miss:
            continue
        whole = [m for m in data.models if all((a, m) in miss for a in data.approaches)]
        rest = [(a, m) for a, m in miss if m not in whole]
        words = DATASET_WORDS.get(ds, chart_name(ds, data.reg["datasets"]))
        if i == 0:
            texts = [f"{m} with every approach" for m in whole] + [
                f"{a.brief} with {join([m for b, m in rest if b == a])}"
                for a in data.approaches
                if any(b == a for b, _ in rest)
            ]
            parts.append(join(texts))
            continue
        text = f"{words} with {join(whole)}" if whole else words
        if whole and rest:
            text += f" and {len(rest)} more setting{'s' if len(rest) > 1 else ''}"
        parts.append(text)
    if data.long_ab is not None and not (long_points(data) or data.breakeven):
        parts.append(f"long packets ({LONG_ID})")
    elif data.long_ab is not None:
        measured = {a for a, _, _, _ in long_points(data)} | {
            a
            for a in data.approaches
            for v in (data.breakeven.get("a"), data.breakeven.get("b"))
            if v and str(v).startswith(f"{a.name} · ")
        }
        waiting = [
            a.brief
            for a in data.approaches
            if a not in measured
            and any(v.startswith(f"{a.name} · ") for v in data.long_ab["values"])
        ]
        if waiting:
            parts.append(f"{join(waiting)} on long packets")
    if not parts:
        return ""
    return f"*Still running: {'; '.join(parts)}. This section fills in as they finish.*"


# ---------------------------------------------------------------------------------
# One subsection per approach


def cost_line(data: Data, a: Approach, ds: str) -> str:
    costs = [
        f"{money(c['mean'])} with {m}"
        for m in data.models
        if (c := data.cell(ds, a, m, data.cost)) and data.cell(ds, a, m, ACCURACY)
    ]
    if not costs:
        return ""
    return f"Classification costs {join(costs)} per 1,000 packets."


def merged_list(data: Data, a: Approach, ds: str) -> list[tuple[str, float]]:
    return [
        (m, v) for m in data.models if (v := data.loss(ds, a, m, "merged")) is not None
    ]


def auroc_best(data: Data, ds: str, a: Approach, model: str) -> bool:
    """Whether the approach's AUROC beats every other approach's with the model by a
    clear gap (an AUROC over one run takes no part)."""
    mine = data.auroc(ds, a, model)
    others = [c for o in data.approaches if o != a and (c := data.auroc(ds, o, model))]
    return bool(mine and others and all(gap_clear(mine, c) for c in others))


def same_class_wins(data: Data, a: Approach) -> list[tuple[str, float]]:
    """The models with which `a` got clearly the most sections right on same-class
    packets."""
    ds = next((d for d in data.datasets if d in PANEL_NAMES), None)
    if ds is None:
        return []
    out = []
    for m in data.models:
        cells = {o: data.cell(ds, o, m, ACCURACY) for o in data.approaches}
        if winner(cells, ACCURACY) == a:
            out.append((m, cells[a]["mean"]))  # type: ignore[index]
    return out


def same_class_merges(data: Data, a: Approach) -> list[tuple[str, float, float]]:
    """Per model: the share of same-class neighbours merged, and of other neighbours."""
    ds = next((d for d in data.datasets if d in PANEL_NAMES), None)
    out = []
    for m in data.models:
        sc = (
            (data.analysed(ds, a, m) or {}).get("same_class_adjacent") or {}
            if ds
            else {}
        )
        same, other = sc.get("pairs_merged"), sc.get("different_class_pairs_merged")
        if isinstance(same, dict) and isinstance(other, dict):
            out.append((m, float(same["mean"]), float(other["mean"])))
    return out


def page_lines(data: Data, a: Approach, ds: str, model: str) -> list[str]:
    lines = []
    merged = merged_list(data, a, ds)
    if merged and all(v <= 0 for _, v in merged):
        lines.append("It never merged two documents into one.")
    elif merged and max(v for _, v in merged) < 0.02:
        lines.append(
            f"It rarely merges two documents into one ({share(max(v for _, v in merged))} "
            "of sections at most)."
        )
    wins = same_class_wins(data, a)
    if wins:
        lines.append(
            "With documents of the same class back to back, it got the most sections "
            "right with " + join([f"{m} ({v:.2f})" for m, v in wins]) + "."
        )
    pred, true = data.predicted_sections(ds, a, model), data.true_sections(ds)
    split = data.loss(ds, a, model, "split")
    if pred is not None and true and split is not None:
        lines.append(
            f"It cuts documents into pieces: with {model} it predicted {pred:.1f} "
            f"sections per packet against {true:.2f} true, so {share(split)} of "
            "sections were split."
        )
    return lines


def holistic_lines(data: Data, a: Approach, ds: str, model: str) -> list[str]:
    lines = []
    measured = [m for m in data.models if data.cell(ds, a, m, data.cost)]
    cheapest = bool(measured) and all(
        data.cell(ds, a, m, data.cost)["mean"]  # type: ignore[index]
        <= min(
            c["mean"] for o in data.approaches if (c := data.cell(ds, o, m, data.cost))
        )
        for m in measured
    )
    rivals = [
        (m, c, mine)
        for m in data.models
        if (mine := data.cell(ds, a, m, ACCURACY))
        for o in data.approaches
        if o != a and (c := data.cell(ds, o, m, ACCURACY))
    ]
    unbeaten = bool(rivals) and not any(gap_clear(c, mine) for _, c, mine in rivals)
    if cheapest and unbeaten:
        lines.append(
            "It is the cheapest approach with every model, and no other approach got "
            "measurably more sections right."
        )
    elif cheapest:
        lines.append("It is the cheapest approach with every model.")
    elif unbeaten:
        lines.append(
            "No other approach got measurably more sections right with any model."
        )
    merged = merged_list(data, a, ds)
    if merged and max(v for _, v in merged) >= 0.05:
        first, *rest = merged
        text = f"{first[0]} merged {share(first[1])} of sections into a neighbour"
        if rest:
            text += ", " + join([f"{m} {share(v)}" for m, v in rest])
        lines.append(f"It can merge neighbouring documents: {text}.")
    sc = [(m, s, o) for m, s, o in same_class_merges(data, a) if s >= 0.05]
    if sc:
        m, s, o = max(sc, key=lambda t: t[1])
        lines.append(
            f"With documents of the same class back to back, {m} merged {share(s)} of "
            f"those pairs into one, against {share(o)} of other pairs."
        )
    gaps = [
        (m, none, total)
        for m in data.models
        for none, total in [data.without_confidence(ds, a, m)]
        if total and none / total >= 0.1
    ]
    if gaps:
        texts = [f"{none} of {total} sections with {m}" for m, none, total in gaps]
        lines.append(
            f"It often returns no confidence: none for {join(texts)} "
            f"({NO_CONFIDENCE_ISSUE})."
        )
    # Claude Haiku 5.5's long-context rate: a holistic call reads the whole packet.
    hk = "Claude Haiku 5.5"
    by_name = {x.name: x for x in approaches(data.ab)}
    hol = pbp = None
    if "Holistic" in by_name and "Page by page" in by_name:
        hol = data.cell(
            ds, by_name["Holistic"], hk, "classification_cost_per_1000_docs"
        )
        pbp = data.cell(
            ds, by_name["Page by page"], hk, "classification_cost_per_1000_docs"
        )
    if hol and pbp and hol["mean"] > pbp["mean"]:
        lines.append(
            f"With {hk} it costs more than page by page ({money(hol['mean'])} against "
            f"{money(pbp['mean'])} per 1,000 packets): a 15-to-20-page packet with its page "
            f"images is one request of more than 100,000 tokens, which {hk} bills at its "
            "long-context rate. Reading text only keeps the call under that."
        )
    return lines


def split_llm_lines(data: Data, a: Approach, ds: str, model: str) -> list[str]:
    lines = []
    scores = [(m, c) for m in data.models if (c := data.auroc(ds, a, m, quoted=True))]
    if scores:
        best = [m for m, _ in scores if auroc_best(data, ds, a, m)]
        text = (
            "Its confidence separates right classes from wrong ones well: AUROC "
            + join([f"{c['mean']:.2f} with {m}" for m, c in scores])
        )
        if best:
            text += f", the highest of any approach with {join(best)}"
        lines.append(text + ".")
    holistic = next((o for o in data.approaches if o.hue == "holistic"), None)
    if holistic is not None:
        compared, better, dearer = 0, False, True
        for m in data.models:
            mine, theirs = (
                data.cell(ds, a, m, data.cost),
                data.cell(ds, holistic, m, data.cost),
            )
            ya, yh = data.cell(ds, a, m, ACCURACY), data.cell(ds, holistic, m, ACCURACY)
            if not (mine and theirs and ya and yh):
                continue
            compared += 1
            better = better or gap_clear(ya, yh)
            dearer = dearer and mine["mean"] > theirs["mean"]
        if compared and dearer and not better:
            lines.append(
                "With every model it cost more than holistic, for no measurable gain in "
                "sections right."
            )
    return lines


def split_dm_lines(data: Data, a: Approach, ds: str, model: str) -> list[str]:
    lines = []
    scores = [(m, c) for m in data.models if (c := data.auroc(ds, a, m, quoted=True))]
    if scores:
        best = [m for m, _ in scores if auroc_best(data, ds, a, m)]
        text = (
            "Its confidence separates right classes from wrong ones well: AUROC "
            + join(
                [f"{c['mean']:.2f} with {m} finding the sections" for m, c in scores]
            )
        )
        if best:
            text += (
                ", the highest of any approach with that model"
                if len(best) == 1 == len(scores)
                else f", the highest of any approach with {join(best)}"
            )
        lines.append(text + ".")
    for m in data.models:
        losses = [
            (k, v) for k, _ in LOSS_KINDS if (v := data.loss(ds, a, m, k)) is not None
        ]
        found = [(k, v) for k, v in losses if k in ("merged", "split")]
        if not found:
            continue
        kind, v = max(found, key=lambda kv: kv[1])
        what = "merged into a neighbour" if kind == "merged" else "split into pieces"
        if v >= 0.05:
            lines.append(
                f"It keeps the section detector's mistakes: with {m}, {share(v)} of "
                f"sections were {what}."
            )
        break
    return lines


def use_when(data: Data, a: Approach, ds: str, pk: Picks | None) -> str:
    if a.hue == "page":
        text = "Use it when most of your documents are a single page"
        wins = same_class_wins(data, a)
        if wins:
            text += (
                f", or with {join([m for m, _ in wins])} when documents of the same "
                "class sit back to back"
            )
        return text + "."
    if a.hue == "holistic":
        if pk and pk.start.approach == a:
            text = "Use it to start"
            if (
                pk.best is not None
                and pk.best.approach == a
                and pk.best.model != pk.start.model
            ):
                text += f", with {pk.best.model} where you need the most sections right"
            return text + "."
        return "Use it when you want one call per packet."
    if a.shape == "triangle":
        tops = [
            m
            for m in data.models
            if (mine := data.auroc(ds, a, m))
            and all(
                mine["mean"] >= c["mean"]
                for o in data.approaches
                if o != a and (c := data.auroc(ds, o, m))
            )
        ]
        if tops:
            return (
                "Use it when you send low-confidence sections to a person for review."
            )
        return "Use it when you want to tune section detection and section classification apart."
    return (
        "Use it when you want a small model on AWS Lambda to classify each section; "
        "give it one-line class descriptions ([SC-12](#planned))."
    )


HOW = {
    "page": (
        "One call per page picks its class; neighbouring pages of the same class become "
        "one section."
    ),
    "holistic": "One call reads the whole packet and returns each document's pages and class.",
    "triangle": "One call finds where each document starts, then one call per section picks its class.",
    "diamond": (
        f"One call finds where each document starts, then the {DECISION_MODEL} picks "
        "each section's class from its first two page images."
    ),
}
LINES: dict[str, Callable[[Data, Approach, str, str], list[str]]] = {
    "circle": page_lines,
    "square": holistic_lines,
    "triangle": split_llm_lines,
    "diamond": split_dm_lines,
}


def subsection(data: Data, a: Approach, pk: Picks | None) -> list[str]:
    ds = data.datasets[0]
    measured = [
        m
        for m in data.models
        if data.analysed(ds, a, m) or data.cell(ds, a, m, ACCURACY)
    ]
    model = (
        pk.start.model if pk and pk.start.model in measured else (measured or [""])[0]
    )
    how = HOW.get(a.hue if a.hue != "split" else a.shape, "")
    body = LINES[a.shape](data, a, ds, model) if measured else []
    cost = cost_line(data, a, ds)
    out = [f"### {a.name}", "", how, ""]
    waiting = [m for m in data.models if m not in measured]
    bullets = [*body, cost] if measured else []
    if bullets and waiting:
        bullets.append(f"With {join(waiting)}: still running.")
    bullets = [b for b in bullets if b]
    if bullets:
        out += [f"- {b}" for b in bullets] + [""]
        out += [use_when(data, a, ds, pk), ""]
    else:
        out += ["*Still running.*", ""]
    return out


# ---------------------------------------------------------------------------------
# Long packets (SC-14)


def long_points(
    data: Data,
) -> list[tuple[Approach, str, dict[str, float], dict[str, float] | None]]:
    """Per SC-14 value measured on long packets: its approach, model, long-packet and
    short-packet section accuracy."""
    lab, cells = data.long_ab, (data.long_results.get("cells") or {})
    if lab is None or not cells:
        return []
    long_ds = next((d for d in lab["datasets"] if "long" in d), None)
    short_ds = next((d for d in lab["datasets"] if d != long_ds), None)
    names = {a.name: a for a in data.approaches} | {a.row: a for a in data.approaches}
    out = []
    for value in lab["values"]:
        row, _, model = value.rpartition(" · ")
        a = names.get(row)
        y = cells.get(f"{long_ds}|{value}|{ACCURACY}")
        if a is None or not isinstance(y, dict):
            continue
        s = cells.get(f"{short_ds}|{value}|{ACCURACY}")
        out.append((a, model, y, s if isinstance(s, dict) else None))
    return out


def long_summary(data: Data) -> list[str]:
    """ "Long packets" from SC-14 alone: its values on 30-to-60-page packets, against
    the same values on DocSplit's 5-to-20-page packets; [] until measured."""
    pts = long_points(data)
    if not pts or data.long_ab is None:
        return []
    long_ds = next(d for d in data.long_ab["datasets"] if "long" in d)
    found = re.search(r"(\d+) to (\d+) pages", data.reg["datasets"][long_ds]["name"])
    where = (
        f"{found.group(1)}–{found.group(2)}-page packets" if found else "long packets"
    )
    models = {m for _, m, _, _ in pts}
    one = next(iter(models)) if len(models) == 1 else ""
    where_with = f"{where} with {one}" if one else where

    def named(a: Approach, m: str) -> str:
        return a.brief if one else f"{a.brief} with {m}"

    top = winner({(a, m): y for a, m, y, _ in pts}, ACCURACY)
    if top:
        y = next(y for b, n, y, _ in pts if (b, n) == top)
        first = (
            f"On {where_with}, {named(*top)} got the most sections right, "
            f"{y['mean']:.2f}."
        )
    else:
        first = f"On {where_with}, no approach got measurably more sections right."
    pairs = [(a, m, y, s) for a, m, y, s in pts if s is not None]
    verbs = {"fell" if y["mean"] < s["mean"] else "rose" for _, _, y, s in pairs}
    text = first
    if len(verbs) == 1:
        moves = [
            f"{s['mean']:.2f} to {y['mean']:.2f} {named(a, m)}" for a, m, y, s in pairs
        ]
        text += (
            f" Against the 5–20-page packets, section accuracy {verbs.pop()} with every "
            f"approach: from {join(moves)}."
        )
    elif pairs:
        moves = [
            f"{'fell' if y['mean'] < s['mean'] else 'rose'} from {s['mean']:.2f} to "
            f"{y['mean']:.2f} {named(a, m)}"
            for a, m, y, s in pairs
        ]
        text += " Against the 5–20-page packets, section accuracy " + join(moves) + "."
    sizes = sample_sizes(
        data.long_results.get("sample") or {}, [long_ds], list(data.long_ab["values"])
    )
    out = ["### Long packets", "", text, ""]
    if long_ds in sizes:
        out += [f"*N = {n_short(sizes[long_ds], 'packets')}.*", ""]
    return out


FRACTIONS = (
    (1 / 2, "half"),
    (1 / 3, "a third"),
    (1 / 4, "a quarter"),
    (1 / 5, "a fifth"),
)


def less_words(saving: float) -> str:
    """ "about a fifth less", or "21% less" when no simple fraction is near."""
    for f, words in FRACTIONS:
        if abs(saving - f) <= 0.03:
            return f"about {words} less"
    return f"{pct(saving)} less"


def signed(v: float) -> str:
    """+0.08, −0.03, or 0.00 for a difference that rounds to nothing."""
    r = round(v, 2)
    return "0.00" if r == 0 else f"{'+' if r > 0 else '−'}{abs(r):.2f}"


def quantile(xs: list[float], q: float) -> float:
    """The q-quantile, interpolating between the two nearest packets."""
    s = sorted(xs)
    at = (len(s) - 1) * q
    lo = math.floor(at)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (at - lo)


@dataclass
class Spread:
    """A set of per-packet figures: n, mean and its standard error, and the quantiles
    a box draws (5th, 25th, 50th, 75th and 95th percentile)."""

    values: list[float]

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def cell(self) -> dict[str, float]:
        se = statistics.stdev(self.values) / math.sqrt(self.n) if self.n > 1 else 0.0
        return {"mean": statistics.fmean(self.values), "se": se}

    @property
    def box(self) -> tuple[float, float, float, float, float]:
        return tuple(quantile(self.values, q) for q in (0.05, 0.25, 0.5, 0.75, 0.95))  # type: ignore[return-value]


@dataclass
class Length:
    """One packet-length group of the paired long-packet analysis."""

    lo: int
    hi: int
    points: list[dict[str, Any]]

    @property
    def label(self) -> str:
        return f"{self.lo}–{self.hi} pages"

    @property
    def delta(self) -> Spread:
        return Spread([float(p["delta"]) for p in self.points])

    def scores(self, key: str) -> Spread | None:
        if not all(key in p for p in self.points):
            return None
        return Spread([float(p[key]) for p in self.points])

    def counts(self) -> tuple[int, int, int]:
        d = self.delta.values
        return (
            sum(x > 1e-9 for x in d),
            sum(abs(x) <= 1e-9 for x in d),
            sum(x < -1e-9 for x in d),
        )


def lengths(data: Data) -> list[Length]:
    """The paired analysis's packets by dataset, shortest first."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in data.breakeven.get("points") or []:
        groups.setdefault(str(p.get("dataset")), []).append(p)
    out = [
        Length(min(p["pages"] for p in pts), max(p["pages"] for p in pts), pts)
        for pts in groups.values()
        if len(pts) > 1
    ]
    return sorted(out, key=lambda g: g.lo)


def long_cells(data: Data, approach: str) -> tuple[dict[str, float] | None, ...]:
    """An SC-14 value's section accuracy and cost on long packets, by approach name."""
    lab, cells = data.long_ab, (data.long_results.get("cells") or {})
    if lab is None:
        return None, None
    long_ds = next((d for d in lab["datasets"] if "long" in d), None)
    a = next((x for x in data.approaches if x.name == approach), None)
    value = next((v for v in lab["values"] if a and v.startswith(f"{a.name} · ")), None)
    found = [cells.get(f"{long_ds}|{value}|{m}") for m in (ACCURACY, data.cost)]
    return tuple(c if isinstance(c, dict) else None for c in found)


def short_cell(data: Data, approach: str) -> dict[str, float] | None:
    """The same value's section accuracy on SC-14's short packets (DocSplit)."""
    lab, cells = data.long_ab, (data.long_results.get("cells") or {})
    if lab is None:
        return None
    short_ds = next((d for d in lab["datasets"] if "long" not in d), None)
    value = next((v for v in lab["values"] if v.startswith(f"{approach} · ")), None)
    c = cells.get(f"{short_ds}|{value}|{ACCURACY}")
    return c if isinstance(c, dict) else None


@dataclass
class LongFacts:
    """What the long-packet subsection says, each figure on one basis: per packet
    (each packet counts once) where the paired analysis carries both scores, else
    SC-14's cells for the absolute accuracies."""

    short: Length
    long: Length
    model: str
    #: (approach name, short-packet accuracy, long-packet accuracy)
    absolute: list[tuple[str, dict[str, float], dict[str, float]]]
    per_packet: bool


def long_facts(data: Data) -> LongFacts | None:
    groups = lengths(data)
    if len(groups) < 2:
        return None
    short, long = groups[0], groups[-1]
    be = data.breakeven
    model = str(be.get("a", "")).rpartition(" · ")[2]
    names = {"a_score": str(be.get("a", "")), "b_score": str(be.get("b", ""))}
    absolute = []
    per_packet = all(short.scores(k) and long.scores(k) for k in names)
    for key, value in sorted(
        names.items(), key=lambda kv: kv[1].startswith("Holistic")
    ):
        approach = value.rpartition(" · ")[0]
        if per_packet:
            s, lg = short.scores(key), long.scores(key)
            absolute.append((approach, s.cell, lg.cell))  # type: ignore[union-attr]
        else:
            s_cell, (l_cell, _) = short_cell(data, approach), long_cells(data, approach)
            if s_cell and l_cell:
                absolute.append((approach, s_cell, l_cell))
    return LongFacts(short, long, model, absolute, per_packet)


def harder_words(f: LongFacts) -> str:
    """ "Long packets are harder for both approaches", "..., clearly so for holistic",
    or "" when no approach's drop clears twice the combined standard error. Said only
    from per-packet scores: the short and long sets are different packets, so it is
    their packet-to-packet spread, not run-to-run noise, that a drop must clear."""
    if not f.per_packet:
        return ""
    clear = [name for name, s, lg in f.absolute if gap_clear(s, lg)]
    if not clear:
        return ""
    if len(clear) == len(f.absolute):
        return "Long packets are harder for both approaches"
    return f"Long packets are harder, clearly so for {join([c.lower() for c in clear])}"


#: The packet-splitting metrics the long-packet comparison shows, in the plot's order:
#: (evaluation key, row name, one plain line on what it measures).
SPLIT_METRICS = (
    (
        "split_accuracy_without_order",
        "Sections exactly right",
        "the share of true sections returned with exactly the right pages and class "
        "(section accuracy)",
    ),
    (
        "final_score",
        "Grouping score",
        "DocSplit's packet score, from 0 to 1: how pages are grouped (V-measure and "
        "Rand index) and ordered, so a nearly right section still earns credit",
    ),
    (
        "boundary_accuracy",
        "Boundary accuracy",
        "the share of sections with exactly the right page range, whatever their class",
    ),
    (
        "v_measure",
        "V-measure",
        "how well pages are grouped into the true documents, from 0 to 1: each section "
        "should hold one document, and each document should sit in one section",
    ),
    (
        "rand_index",
        "Rand index",
        "the share of pairs of pages put together, or kept apart, as in the true "
        "documents",
    ),
    (
        "avg_ordering_score",
        "Ordering",
        "how close each document's pages are to their true order, from 0 to 1 "
        "(Kendall's tau, averaged over documents)",
    ),
    (
        "page_level_accuracy",
        "Page accuracy",
        "the share of pages with the right class",
    ),
    (
        "label_accuracy",
        "Label accuracy",
        "of the sections with the right page range, the share with the right class",
    ),
)
STRICT, GROUPING = "split_accuracy_without_order", "final_score"
STICKLER = "https://awslabs.github.io/stickler/Guides/Document_Packet_Splitting/"


def paired(data: Data, pair: str, ds: str, key: str) -> dict[str, float] | None:
    """A paired difference from the analysis (`a_minus_b`, `c_minus_a`), or None."""
    cell = ((data.breakeven.get("metrics") or {}).get(pair) or {}).get(ds, {}).get(key)
    if not isinstance(cell, dict) or cell.get("se") is None:
        return None
    return {"mean": float(cell["mean"]), "se": float(cell["se"]), "n": int(cell["n"])}


def pm(c: dict[str, float]) -> str:
    return f"{signed(c['mean'])} ± {2 * c['se']:.2f}"


def side(c: dict[str, float] | None) -> int:
    """+1 where holistic is clearly ahead, -1 clearly behind, 0 within 2·SE."""
    if c is None:
        return 0
    zero = {"mean": 0.0, "se": 0.0}
    return 1 if gap_clear(c, zero) else -1 if gap_clear(zero, c) else 0


def cost_times(ratio: float) -> str:
    if 1.4 <= ratio <= 1.6:
        return "about half again the cost"
    if 1.9 <= ratio <= 2.1:
        return "about twice the cost"
    return f"{ratio:.1f} times the cost"


def long_text(data: Data, f: LongFacts) -> list[str]:
    """The takeaway: what to use, then why (the strict measure and the measures that
    give partial credit), then split then classify. Each claim only as far as twice
    the combined standard error carries it."""
    ds_short, ds_long = f.short.points[0]["dataset"], f.long.points[0]["dataset"]
    strict_s, strict_l = (
        paired(data, "a_minus_b", d, STRICT) for d in (ds_short, ds_long)
    )
    if strict_s is None or strict_l is None:
        strict_s, strict_l = f.short.delta.cell, f.long.delta.cell
    group_l = paired(data, "a_minus_b", ds_long, GROUPING)
    _, ca = long_cells(data, "Holistic")
    _, cb = long_cells(data, "Page by page")
    pk = picks(data.points(data.datasets[0]))
    starts = bool(pk and pk.start.approach.name == "Holistic")

    answer = []
    grouping_lead = side(group_l) > 0 and side(strict_l) == 0
    if starts and side(strict_l) >= 0 and side(group_l) >= 0:
        text = "Holistic stays a good place to start on long packets"
        if grouping_lead:
            text += (
                ": there its advantage is in grouping pages, not in getting every "
                "boundary exactly right"
            )
        answer.append(text + ".")
    if ca and cb and ca["mean"] < cb["mean"]:
        answer.append(
            f"It costs {less_words(1 - ca['mean'] / cb['mean'])} than page by page "
            f"({money(ca['mean'])} against {money(cb['mean'])} of classification per "
            "1,000 long packets)."
        )
    elif ca and cb and ca["mean"] > cb["mean"]:
        answer.append(
            f"On Claude Haiku 5.5 it costs {times_text(ca['mean'] / cb['mean'])} as much "
            f"as page by page ({money(ca['mean'])} against {money(cb['mean'])} of "
            "classification per 1,000 long packets), because a 20-page window with page "
            "images goes over the 100,000 tokens above which Claude Haiku 5.5 bills its "
            "long-context rate; a 10- or 15-page window keeps most calls under it "
            "([SC-04](#how-long-a-packet-can-holistic-classification-handle)), as does "
            "reading text only."
        )

    why = []
    by = {name: (s, lg) for name, s, lg in f.absolute}
    hol, page = by.get("Holistic"), by.get("Page by page")
    if hol and page:
        a, b = pct(hol[1]["mean"]), pct(page[1]["mean"])
        on_long = (
            f"both got {a} of sections exactly right"
            if a == b
            else f"holistic got {a} of sections exactly right and page by page {b}"
        )
        text = (
            f"on {f.long.label} {on_long}, against {pct(hol[0]['mean'])} for holistic "
            f"and {pct(page[0]['mean'])} for page by page on {f.short.label}"
        )
        lead = harder_words(f)
        why.append(f"{lead}: {text}." if lead else f"{text[:1].upper()}{text[1:]}.")
    if side(strict_s) > 0 and side(strict_l) == 0:
        why.append(
            f"On that strict measure, holistic's lead on short packets ({pm(strict_s)}) "
            f"isn't measurable on long ones ({pm(strict_l)})."
        )
    elif side(strict_l) > 0:
        why.append(
            f"Holistic got more sections exactly right on long packets too ({pm(strict_l)})."
        )
    if group_l is not None and side(group_l) > 0:
        names = {k: n for k, n, _ in SPLIT_METRICS}
        also = [
            names[k]
            for k in ("v_measure", "rand_index", "avg_ordering_score")
            if side(paired(data, "a_minus_b", ds_long, k)) > 0
        ]
        text = (
            "But on the grouping score, which gives credit for nearly right sections, "
            f"holistic still leads clearly on long packets ({pm(group_l)})"
        )
        if also:
            text += ", as it does on " + join(
                [a if a != "Ordering" else "ordering" for a in also]
            )
        why.append(text + ".")
    same = [
        n
        for k, n, _ in SPLIT_METRICS
        if k in ("page_level_accuracy",)
        and all(
            (c := paired(data, "a_minus_b", d, k)) is not None and side(c) == 0
            for d in (ds_short, ds_long)
        )
    ]
    label = paired(data, "a_minus_b", ds_long, "label_accuracy")
    parts = [f"{join(same)} is the same"] if same else []
    if label is not None and side(label) < 0:
        parts.append(
            "page by page labels the sections it gets right slightly better (label "
            f"accuracy {pm(label)})"
        )
    if parts:
        text = ", and ".join(parts)
        why.append(f"{text[:1].upper()}{text[1:]}.")

    split = []
    names = {k: n for k, n, _ in SPLIT_METRICS}
    found = {k: paired(data, "c_minus_a", ds_long, k) for k in names}
    split_name = next((a.name for a in data.approaches if a.shape == "triangle"), "")
    _, cc = long_cells(data, split_name)
    key = [found.get(STRICT), found.get(GROUPING)]
    if all(key) and all(side(c) == 0 for c in key):
        text = (
            "Splitting first, then having an LLM classify each section, got no "
            "measurably more sections exactly right than holistic on long packets, nor a "
            "measurably better grouping score"
        )
        ahead = [
            f"{names[k].lower()} ({pm(c)})"
            for k, c in found.items()
            if c and k not in (STRICT, GROUPING) and side(c) > 0
        ]
        behind = [
            f"{names[k].lower()} ({pm(c)})"
            for k, c in found.items()
            if c and k not in (STRICT, GROUPING) and side(c) < 0
        ]
        if ahead:
            text += f"; it was narrowly ahead on {join(ahead)}"
        if behind:
            text += f"{';' if not ahead else ','} behind on {join(behind)}"
        if cc and ca:
            text += (
                f", at {cost_times(cc['mean'] / ca['mean'])} ({money(cc['mean'])} "
                f"against {money(ca['mean'])})"
            )
        split.append(text + ".")
    return [" ".join(p) for p in (answer, why, split) if p]


def forest_chart(data: Data, f: LongFacts) -> tuple[str, str] | None:
    """Holistic minus page by page on each packet-splitting metric, two markers per
    row (short and long packets) with ±2·SE bars, a zero line."""
    ds_short, ds_long = f.short.points[0]["dataset"], f.long.points[0]["dataset"]
    rows = [
        (
            name,
            paired(data, "a_minus_b", ds_short, k),
            paired(data, "a_minus_b", ds_long, k),
        )
        for k, name, _ in SPLIT_METRICS
    ]
    rows = [r for r in rows if r[1] or r[2]]
    if not rows:
        return None
    strict_l = paired(data, "a_minus_b", ds_long, STRICT)
    group_l = paired(data, "a_minus_b", ds_long, GROUPING)
    title = (
        f"With {f.model}, holistic still groups long packets better than page by page"
        if side(group_l) > 0 and side(strict_l) == 0
        else f"With {f.model}, holistic minus page by page on each splitting measure"
    )
    sub = (
        "Holistic minus page by page, mean over the packets each scored, ±2 standard "
        f"errors · {f.short.delta.n} packets of {f.short.label}, {f.long.delta.n} of "
        f"{f.long.label}"
    )
    ends = [
        c["mean"] + s * 2 * c["se"] for _, *cs in rows for c in cs if c for s in (-1, 1)
    ]
    lo = math.floor(min(min(ends), 0) * 20 - 0.5) / 20
    hi = math.ceil(max(max(ends), 0) * 20 + 0.5) / 20
    ticks = [round(lo + i * 0.05, 10) for i in range(int(round((hi - lo) * 20)) + 1)]
    x0, x1 = 170.0, W - 20.0
    row_h, top = 30.0, 104.0
    y_end = top + row_h * len(rows)

    def px(v: float) -> float:
        return x0 + (v - lo) / (hi - lo) * (x1 - x0)

    body = [
        f'<text class="ctitle" x="0" y="18">{escape(title)}</text>',
        f'<text class="sub" x="0" y="40">{escape(sub)}</text>',
        f'<circle class="short" cx="6" cy="62" r="4.5"/>'
        f'<text class="label" x="16" y="66">{escape(f.short.label)}</text>'
        f'<circle class="est" cx="{16 + len(f.short.label) * CHAR + 24:.1f}" cy="62" r="4.5"/>'
        f'<text class="label" x="{26 + len(f.short.label) * CHAR + 24:.1f}" y="66">'
        f"{escape(f.long.label)}</text>",
    ]
    for t in ticks:
        body.append(
            f'<line class="grid" x1="{px(t):.1f}" x2="{px(t):.1f}" y1="{top - 6:.1f}" y2="{y_end:.1f}"/>'
            f'<text class="tick" x="{px(t):.1f}" y="{y_end + 16:.1f}" text-anchor="middle">'
            f"{'0' if abs(t) < 1e-9 else signed(t)}</text>"
        )
    body.append(
        f'<line class="zero" x1="{px(0):.1f}" x2="{px(0):.1f}" y1="{top - 14:.1f}" y2="{y_end:.1f}"/>'
        f'<text class="baselinelabel" x="{px(0):.1f}" y="{top - 18:.1f}" text-anchor="middle">No difference</text>'
        f'<line class="axis" x1="{x0:.1f}" x2="{x1:.1f}" y1="{y_end:.1f}" y2="{y_end:.1f}"/>'
        f'<text class="axislabel" x="{x1:.1f}" y="{y_end + 36:.1f}" text-anchor="end">holistic better →</text>'
        f'<text class="axislabel" x="{x0:.1f}" y="{y_end + 36:.1f}">← page by page better</text>'
    )
    desc = []
    for i, (name, cs, cl) in enumerate(rows):
        cy = top + row_h * (i + 0.5)
        body.append(f'<text class="label" x="0" y="{cy + 4:.1f}">{escape(name)}</text>')
        for c, dy, cls, bar in ((cs, -5, "short", "ebar2"), (cl, 5, "est", "ebar")):
            if not c:
                continue
            e = 2 * c["se"]
            y = cy + dy
            label = (
                f"{name}, {f.short.label if cls == 'short' else f.long.label}: {pm(c)}"
            )
            body.append(
                f"<g><title>{escape(label)}</title>"
                f'<line class="{bar}" x1="{px(c["mean"] - e):.1f}" x2="{px(c["mean"] + e):.1f}" '
                f'y1="{y:.1f}" y2="{y:.1f}"/>'
                f'<circle class="{cls}" cx="{px(c["mean"]):.1f}" cy="{y:.1f}" r="4.5"/></g>'
            )
            desc.append(label + ".")
    height = y_end + 46
    alt = (
        f"{title}: holistic minus page by page on each packet-splitting metric, with two "
        f"standard errors either side, on {f.short.label} and {f.long.label}"
    )
    return svg(W, height, title, " ".join(desc), body), alt


def metric_definitions() -> list[str]:
    out = ["<details>", "<summary>What these metrics mean</summary>", ""]
    out += [f"- *{name}:* {text}." for _, name, text in SPLIT_METRICS]
    out += [
        "",
        "The page's [metric definitions](#questions) and Stickler's [Document Packet "
        f"Splitting Evaluation]({STICKLER}) have more.",
        "",
        "</details>",
        "",
    ]
    return out


BOX_W = 24.0
LONG_W = 792.0
PANEL_GAP = 40.0


def box_svg(
    cx: float, sp: Spread, py: Callable[[float], float], cls: str, strip: str, seed: int
) -> list[str]:
    """A box (25th to 75th percentile, a line at the median), whiskers (5th to 95th),
    the packets as a faint strip behind, nudged sideways by a fixed amount."""
    p5, q1, med, q3, p95 = sp.box
    h = BOX_W / 2
    out = []
    for i, v in enumerate(sp.values):
        nudge = (((i + seed) * 7919) % 17 - 8) / 8 * (h + 4)
        out.append(
            f'<circle class="{strip}" cx="{cx + nudge:.1f}" cy="{py(v):.1f}" r="2.3"/>'
        )
    out.append(
        f'<line class="whisker" x1="{cx:.1f}" x2="{cx:.1f}" y1="{py(p95):.1f}" y2="{py(q3):.1f}"/>'
        f'<line class="whisker" x1="{cx:.1f}" x2="{cx:.1f}" y1="{py(q1):.1f}" y2="{py(p5):.1f}"/>'
        f'<line class="whisker" x1="{cx - 5:.1f}" x2="{cx + 5:.1f}" y1="{py(p95):.1f}" y2="{py(p95):.1f}"/>'
        f'<line class="whisker" x1="{cx - 5:.1f}" x2="{cx + 5:.1f}" y1="{py(p5):.1f}" y2="{py(p5):.1f}"/>'
        f'<rect class="{cls}" x="{cx - h:.1f}" y="{py(q3):.1f}" width="{BOX_W:.0f}" '
        f'height="{max(py(q1) - py(q3), 1):.1f}" rx="3"/>'
        f'<line class="median" x1="{cx - h:.1f}" x2="{cx + h:.1f}" y1="{py(med):.1f}" y2="{py(med):.1f}"/>'
    )
    return out


def mean_svg(
    cx: float, cell: dict[str, float], py: Callable[[float], float], dot: str
) -> str:
    e = 2 * cell["se"]
    return (
        f'<line class="ebar" x1="{cx:.1f}" x2="{cx:.1f}" y1="{py(cell["mean"] + e):.1f}" '
        f'y2="{py(cell["mean"] - e):.1f}"/>' + dot
    )


def wrap_words(text: str, chars: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > chars:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + ([line] if line else [])


def long_chart(data: Data, f: LongFacts) -> tuple[str, str]:
    """Two panels: each approach's section accuracy per packet, by length; and the
    paired difference, holistic minus page by page, by length. Boxes when the paired
    analysis carries each packet's scores; the absolute panel falls back to a dot
    and ±2·SE bar per approach (SC-14's cells) when it does not."""
    groups = [f.short, f.long]
    by = {name: (s, lg) for name, s, lg in f.absolute}
    order = [a for a in data.approaches if a.name in by]
    keys = {
        str(data.breakeven.get("a", "")).rpartition(" · ")[0]: "a_score",
        str(data.breakeven.get("b", "")).rpartition(" · ")[0]: "b_score",
    }
    left_w = (LONG_W - PANEL_GAP) * 0.5
    right_x = left_w + PANEL_GAP
    right_w = LONG_W - right_x
    top, plot_h = 98.0, 250.0
    y0, y1 = top, top + plot_h
    body: list[str] = []
    desc: list[str] = []

    zero = {"mean": 0.0, "se": 0.0}
    harder = harder_words(f)
    t1 = (
        f"With {f.model}, {harder[:1].lower()}{harder[1:]}"
        if harder
        else f"With {f.model}, section accuracy by packet length"
    )
    ds, dl = f.short.delta.cell, f.long.delta.cell
    if gap_clear(ds, zero) and not (gap_clear(dl, zero) or gap_clear(zero, dl)):
        t2 = f"With {f.model}, holistic's lead on long packets is unclear"
    else:
        t2 = f"With {f.model}, holistic minus page by page, by packet length"

    def heading(x: float, width: float, title: str, sub: str) -> None:
        y = 16.0
        for line in wrap_words(title, int(width / 7.4)):
            body.append(
                f'<text class="ctitle" x="{x:.1f}" y="{y:.1f}">{escape(line)}</text>'
            )
            y += 19
        for line in wrap_words(sub, int(width / 6.1)):
            body.append(
                f'<text class="sub" x="{x:.1f}" y="{y + 2:.1f}">{escape(line)}</text>'
            )
            y += 16

    # Left: section accuracy per packet, 0 to 100%.
    x0, x1 = 44.0, left_w
    heading(
        0,
        left_w,
        t1,
        "Sections exactly right per packet"
        if f.per_packet
        else "Sections exactly right, over all sections of the runs",
    )
    lx = 0.0
    for a in order:
        body.append(mark(a, lx + 6, top - 18))
        body.append(
            f'<text class="label" x="{lx + 16:.1f}" y="{top - 14:.1f}">{escape(a.name)}</text>'
        )
        lx += 16 + len(a.name) * CHAR + 20

    def py1(v: float) -> float:
        return y1 - v * (y1 - y0)

    for t in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        body.append(
            f'<line class="grid" x1="{x0}" x2="{x1:.1f}" y1="{py1(t):.1f}" y2="{py1(t):.1f}"/>'
            f'<text class="tick" x="{x0 - 6}" y="{py1(t) + 4:.1f}" text-anchor="end">{pct(t)}</text>'
        )
    body.append(
        f'<line class="axis" x1="{x0}" x2="{x1:.1f}" y1="{y1:.1f}" y2="{y1:.1f}"/>'
    )
    gw = (x1 - x0) / len(groups)
    for gi, g in enumerate(groups):
        gx = x0 + gw * (gi + 0.5)
        body.append(
            f'<text class="klabel" x="{gx:.1f}" y="{y1 + 34:.1f}" text-anchor="middle">{g.label}</text>'
        )
        for ai, a in enumerate(order):
            cx = gx + (ai - (len(order) - 1) / 2) * (BOX_W + 26)
            s, lg = by[a.name]
            cell = s if gi == 0 else lg
            sp = g.scores(keys.get(a.name, "")) if f.per_packet else None
            if sp is not None:
                body += box_svg(
                    cx, sp, py1, f"bx ap-{a.hue}", f"strip ap-{a.hue}", gi * 31 + ai
                )
            body.append(
                f"<g><title>{escape(a.name)}, {g.label}: mean {cell['mean']:.2f} ± "
                f"{2 * cell['se']:.2f}</title>"
                + mean_svg(cx, cell, py1, mark(a, cx, py1(cell["mean"]), 4.5))
                + "</g>"
            )
            if sp is not None:
                body.append(
                    f'<text class="tick" x="{cx:.1f}" y="{y1 + 16:.1f}" '
                    f'text-anchor="middle">n = {sp.n}</text>'
                )
            desc.append(f"{a.name}, {g.label}: mean {cell['mean']:.2f}.")

    # Right: the paired difference per packet, with a zero line.
    rx0, rx1 = right_x + 40, LONG_W - 6
    heading(
        right_x,
        right_w,
        t2,
        "Holistic minus page by page, sections exactly right",
    )
    ext = max(abs(v) for g in groups for v in g.delta.values)
    y_ext = max(0.2, math.ceil(ext * 10 + 1e-9) / 10)

    def py2(v: float) -> float:
        return y1 - (v + y_ext) / (2 * y_ext) * (y1 - y0)

    steps = int(y_ext / 0.2 + 1e-9)
    for i in range(-steps, steps + 1):
        t = round(i * 0.2, 10)
        body.append(
            f'<line class="grid" x1="{rx0:.1f}" x2="{rx1:.1f}" y1="{py2(t):.1f}" y2="{py2(t):.1f}"/>'
            f'<text class="tick" x="{rx0 - 6:.1f}" y="{py2(t) + 4:.1f}" text-anchor="end">{signed(t)[:-1] if t else "0"}</text>'
        )
    body.append(
        f'<line class="axis" x1="{rx0:.1f}" x2="{rx1:.1f}" y1="{y1:.1f}" y2="{y1:.1f}"/>'
        f'<line class="zero" x1="{rx0:.1f}" x2="{rx1:.1f}" y1="{py2(0):.1f}" y2="{py2(0):.1f}"/>'
    )
    gw2 = (rx1 - rx0) / len(groups)
    # The zero line's name sits between the boxes, where no packet is drawn.
    body.append(
        f'<text class="baselinelabel halo" x="{rx0 + gw2:.1f}" y="{py2(0) - 5:.1f}" '
        'text-anchor="middle">No difference</text>'
    )
    for gi, g in enumerate(groups):
        cx = rx0 + gw2 * (gi + 0.5)
        sp = g.delta
        body += box_svg(cx, sp, py2, "bx neutral", "packet", 101 + gi)
        cell = sp.cell
        dot = (
            f'<circle class="est" cx="{cx:.1f}" cy="{py2(cell["mean"]):.1f}" r="4.5"/>'
        )
        body.append(
            f"<g><title>{g.label}: mean {signed(cell['mean'])} ± {2 * cell['se']:.2f}</title>"
            + mean_svg(cx, cell, py2, dot)
            + "</g>"
        )
        better, tie, worse = g.counts()
        body.append(
            f'<text class="tick" x="{cx:.1f}" y="{y1 + 16:.1f}" text-anchor="middle">n = {sp.n}</text>'
            f'<text class="klabel" x="{cx:.1f}" y="{y1 + 34:.1f}" text-anchor="middle">{g.label}</text>'
            f'<text class="prices" x="{cx:.1f}" y="{y1 + 50:.1f}" text-anchor="middle">'
            f"better {better} · tie {tie} · worse {worse}</text>"
        )
        desc.append(
            f"{g.label}: holistic minus page by page {signed(cell['mean'])} ± "
            f"{2 * cell['se']:.2f}; holistic better on {better}, tie {tie}, worse {worse}."
        )
    foot = (
        "Boxes: middle half of the packets, line at the median. Whiskers: 5th to 95th "
        "percentile. Dots: the mean, with a bar of ±2 standard errors."
    )
    if not f.per_packet:
        foot = (
            "Left: section accuracy over all sections, ±2 standard errors. Right: boxes, "
            "middle half of the packets, line at the median; whiskers, 5th to 95th "
            "percentile; dot, the mean, ±2 standard errors."
        )
    y = y1 + 72
    for line in wrap_words(foot, int(LONG_W / 5.3)):
        body.append(f'<text class="prices" x="0" y="{y:.1f}">{escape(line)}</text>')
        y += 13
    height = y - 3
    title = f"{t1}; {t2[0].lower()}{t2[1:]}"
    alt = (
        f"{t1}, and {t2[len('With ' + f.model + ', ') :] if t2.startswith('With ') else t2}: "
        f"section accuracy {'per packet' if f.per_packet else 'over all sections'} for "
        f"holistic and page by page on {f.short.label} "
        f"and {f.long.label}, and the paired difference per packet, as boxes with the "
        "mean and two standard errors"
    )
    return svg(LONG_W, height, title, " ".join(desc), body), alt


def long_table(data: Data, f: LongFacts) -> list[str]:
    """The long-packet figures, as the chart draws them."""
    by = {name: (s, lg) for name, s, lg in f.absolute}
    head = ["Packets", "Scored by both"]
    head += [f"{n} section accuracy" for n in by]
    head += [
        "Holistic minus page by page",
        "Median difference",
        "Holistic better · tie · worse",
    ]
    rows = []
    for gi, g in enumerate((f.short, f.long)):
        d = g.delta
        rows.append(
            [
                g.label,
                str(d.n),
                *(
                    f"{(s if gi == 0 else lg)['mean']:.2f} ± {2 * (s if gi == 0 else lg)['se']:.2f}"
                    for s, lg in by.values()
                ),
                f"{signed(d.cell['mean'])} ± {2 * d.cell['se']:.2f}",
                signed(quantile(d.values, 0.5)),
                " · ".join(str(c) for c in g.counts()),
            ]
        )
    basis = (
        "each packet counted once, its runs averaged"
        if f.per_packet
        else "accuracies over all sections from SC-14's runs; differences per packet"
    )
    return [
        f"**Long packets, {f.model}**",
        "",
        "| " + " | ".join(head) + " |",
        "|" + "---|" * len(head),
        *("| " + " | ".join(r) + " |" for r in rows),
        "",
        f"*{basis}; ± is twice the standard error.*",
        "",
    ]


def long_caption(per_packet: bool) -> str:
    boxes = (
        "boxes span the middle half of the packets, with a line at the median; "
        "whiskers reach the 5th and 95th percentiles; the dot is the mean, with a bar "
        "of ±2 standard errors"
    )
    if per_packet:
        return f"*{boxes[:1].upper()}{boxes[1:]}.*"
    return (
        f"*Right: {boxes}. Left: section accuracy over all sections of the runs, ±2 "
        "standard errors.*"
    )


def long_section(data: Data) -> tuple[list[str], dict[str, str]]:
    """ "Long packets": the takeaway, the box views, the per-metric comparison and the
    metrics' definitions when fit_length_breakeven.py has run, else the SC-14
    summary; nothing until either."""
    f = long_facts(data)
    if f is None:
        return long_summary(data), {}
    chart, alt = long_chart(data, f)
    charts = {LONG_SVG: chart}
    out = ["### Long packets", ""]
    for para in long_text(data, f):
        out += [para, ""]
    out += [
        f"![{alt}]({CHARTS_FROM_PAGES}/{LONG_SVG})",
        "",
        long_caption(f.per_packet),
        "",
    ]
    forest = forest_chart(data, f)
    if forest:
        charts[METRICS_SVG] = forest[0]
        out += [
            f"![{forest[1]}]({CHARTS_FROM_PAGES}/{METRICS_SVG})",
            "",
            "*Each dot is the mean of the packets' paired differences, with a bar of ±2 "
            "standard errors; a bar that crosses the zero line is no measurable "
            "difference.*",
            "",
            *metric_definitions(),
        ]
    out += service_limits()
    return out, charts


def service_limits() -> list[str]:
    """How much one holistic call can hold: the Bedrock and model limits that set the
    accelerator's packet window, with the per-page token counts measured on DocSplit."""
    return [
        "**How much one holistic call can hold.** A holistic call sends every page of the "
        "packet in one request, so its size is set by service limits, not by accuracy:",
        "",
        "- **Images:** Amazon Bedrock sets no limit of its own on how many images a request "
        "carries; each model's limits apply ([Bedrock API restrictions]"
        "(https://docs.aws.amazon.com/bedrock/latest/userguide/inference-api-restrictions.html)). "
        "For Claude that is a request of up to 32 MB with images up to 8,000 pixels, and in a "
        "request with more than 20 images every image must be 2,000 pixels or smaller "
        "([Claude vision limits](https://platform.claude.com/docs/en/build-with-claude/vision#request-limits)). "
        "The accelerator's packet window is 20 pages by default so pages keep their full "
        "resolution; a wider window works, at 2,000 pixels a page.",
        "- **Tokens:** in our runs a page cost about 6,100 tokens with its image and 1,200 as "
        "text alone on the Claude models, and about 1,050 with its image on Amazon Nova 2 Lite. "
        "[Claude Haiku 5.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html), "
        "[Claude Sonnet 5.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html) "
        "and [Amazon Nova 2 Lite](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-2-lite.html) "
        "each read up to 1 million tokens, so text alone fits hundreds of pages.",
        "- **Price:** Claude Haiku 5.5 bills a prompt of more than 100,000 tokens at higher "
        "long-context rates ([model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html#model-card-anthropic-claude-haiku-5-5-pricing)); "
        "in the Bedrock price list they are five times the standard rates. That is about 16 "
        "pages with images, or about 80 pages of text alone.",
        "",
        "A packet longer than that is not a reason to switch to page by page: the accelerator "
        "reads it in overlapping 20-page windows and merges them "
        "([Packet granularity and windowing](../classification.md#packet-granularity-images-few-shot-examples-and-windowing)). "
        "Prefer page by page when a single document in your packets is longer than the window "
        "and its pages need images.",
        "",
    ]


# ---------------------------------------------------------------------------------
# Charts

W = 792
HUE_CSS = (
    "  .viz { "
    + " ".join(f"--ap-{k}: {light};" for k, (light, _) in HUES.items())
    + " }\n"
    "  @media (prefers-color-scheme: dark) {\n"
    '    :root:where(:not([data-theme="light"])) .viz, svg.viz:root { '
    + " ".join(f"--ap-{k}: {dark};" for k, (_, dark) in HUES.items())
    + " }\n  }\n"
    '  :root[data-theme="dark"] .viz { '
    + " ".join(f"--ap-{k}: {dark};" for k, (_, dark) in HUES.items())
    + " }\n"
    + "".join(
        f"  .viz .ap-{k} {{ fill: var(--ap-{k}); }}\n"
        f"  .viz .band.ap-{k} {{ fill: var(--ap-{k}); opacity: 0.14; }}\n"
        for k in HUES
    )
    + "  .viz .mlabel { font-size: 12px; font-weight: 600; fill: var(--ink); }\n"
    "  .viz .klabel { font-size: 12px; fill: var(--ink-2); }\n"
    "  .viz .gname { font-size: 12px; font-weight: 600; fill: var(--ink); }\n"
    "  .viz .val { font-size: 11px; fill: var(--ink-2); font-variant-numeric: tabular-nums; }\n"
    "  .viz .packet { fill: var(--muted); opacity: 0.35; }\n"
    "  .viz .est { fill: var(--ink); stroke: var(--ring); stroke-width: 2; }\n"
    "  .viz .ebar { stroke: var(--ink); stroke-width: 2; stroke-linecap: round; }\n"
    "  .viz .span { stroke: var(--ink-2); stroke-width: 2; stroke-linecap: round; }\n"
    "  .viz .zero { stroke: var(--ink-2); stroke-width: 1; }\n"
    "  .viz .bx { opacity: 0.22; }\n"
    "  .viz .bx.neutral { fill: var(--ink-2); opacity: 0.14; }\n"
    "  .viz .strip { opacity: 0.3; }\n"
    "  .viz .whisker { stroke: var(--ink-2); stroke-width: 1.5; }\n"
    "  .viz .median { stroke: var(--ink); stroke-width: 2; }\n"
    "  .viz .short { fill: var(--ring); stroke: var(--ink-2); stroke-width: 2; }\n"
    "  .viz .ebar2 { stroke: var(--ink-2); stroke-width: 1.5; stroke-linecap: round; }\n"
)
CHAR = 6.6  # average advance of 12px system-ui
CHAR_BOLD = 7.0

Rect = tuple[float, float, float, float]


def mark(a: Approach, cx: float, cy: float, r: float = 5.0, extra: str = "") -> str:
    """The approach's marker: its hue, its shape, a ring of the surface around it."""
    cls = f"dot ap-{a.hue}{extra}"
    if a.shape == "triangle":
        h = r * 1.35
        return (
            f'<path class="{cls}" d="M{cx:.1f} {cy - h:.1f}L{cx + h * 0.95:.1f} '
            f'{cy + h * 0.65:.1f}L{cx - h * 0.95:.1f} {cy + h * 0.65:.1f}Z"/>'
        )
    return marker(a.shape, cx, cy, r, cls)


def legend(items: list[Approach], y: float) -> str:
    out, x = [], 0.0
    for a in items:
        out.append(mark(a, x + 6, y - 4))
        out.append(
            f'<text class="label" x="{x + 16:.1f}" y="{y:.1f}">{escape(a.legend)}</text>'
        )
        x += 16 + len(a.legend) * CHAR + 22
    return "".join(out)


def svg(width: float, height: float, title: str, desc: str, body: list[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" role="img">'
        f"<title>{escape(title)}</title><desc>{escape(desc)}</desc>"
        f"<style>{STYLE}{HUE_CSS}</style>" + "".join(body) + "</svg>\n"
    )


def hits(r: Rect, others: list[Rect]) -> bool:
    return any(
        r[0] < o[2] and o[0] < r[2] and r[1] < o[3] and o[1] < r[3] for o in others
    )


def inside(r: Rect, b: Rect) -> bool:
    return r[0] >= b[0] and r[2] <= b[2] and r[1] >= b[1] and r[3] <= b[3]


def place(
    text: str,
    box: Rect,
    blocked: list[Rect],
    bounds: Rect,
    char: float = CHAR_BOLD,
    below_first: bool = False,
) -> tuple[float, float, str, Rect]:
    """Where a label goes: above the box it names, else below, right, left (below
    first when `below_first`). Returns x, baseline y, text-anchor and its box."""
    w = len(text) * char
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    above = [(cx + dx, box[1] - 7, "middle") for dx in (0.0, -w / 3, w / 3)]
    below = [(cx + dx, box[3] + 15, "middle") for dx in (0.0, -w / 3, w / 3)]
    tries = below + above if below_first else above + below
    tries += [(box[2] + 8, cy + 4, "start"), (box[0] - 8, cy + 4, "end")]

    def rect(x: float, y: float, anchor: str) -> Rect:
        x0 = x - w / 2 if anchor == "middle" else x if anchor == "start" else x - w
        return (x0 - 2, y - 12, x0 + w + 2, y + 3)

    for x, y, anchor in tries:
        r = rect(x, y, anchor)
        if inside(r, bounds) and not hits(r, blocked):
            return x, y, anchor, r
    x, y, anchor = tries[0]
    x = min(max(x, bounds[0] + w / 2), bounds[2] - w / 2)
    return x, y, anchor, rect(x, y, anchor)


@dataclass
class Segment:
    """One stretch of the cost axis: its range in dollars and its pixel span."""

    lo: float
    hi: float
    x0: float
    x1: float
    ticks: list[float]

    def px(self, v: float) -> float:
        return self.x0 + (v - self.lo) / (self.hi - self.lo) * (self.x1 - self.x0)


def money_tick(t: float) -> str:
    return f"${t:,.0f}" if abs(t - round(t)) < 1e-9 else f"${t:,.2f}"


#: The note beside the answer's starting point, on the first panel.
START_NOTE = "Good place to start"


def headline_chart(
    data: Data, panels: list[str], title: str, start: str | None = None
) -> tuple[str, str]:
    """Sections exactly right against classification cost, a panel per dataset, one
    point per approach and model, `start` (a value) ringed and noted on the first
    panel; returns the SVG and its alt text."""
    metrics = data.reg["metrics"]
    pts_by = {ds: data.points(ds) for ds in panels}
    every = [p for ds in panels for p in pts_by[ds]]
    upper = cost_split([p.x["mean"] for p in every])
    lower_x = [p.x["mean"] for p in every if upper is None or p.x["mean"] < upper]
    upper_x = [p.x["mean"] for p in every if upper is not None and p.x["mean"] >= upper]
    left, right = 46.0, W - 16.0
    gap = 44.0 if upper_x else 0.0
    width = right - left - gap
    split_at = left + width * (0.64 if upper_x else 1.0)
    lt = nice_ticks(0, max(lower_x) * 1.08, 4)
    segs = [Segment(0.0, lt[-1], left, split_at, lt)]
    if upper_x:
        ut = nice_ticks(min(upper_x) * 0.88, max(upper_x) * 1.06, 4)
        segs.append(Segment(ut[0], ut[-1], split_at + gap, right, ut))
    ys = [p.y["mean"] + s * 2 * p.y.get("se", 0) for p in every for s in (-1, 1)]
    y_lo = math.floor((min(ys) - 0.02) * 10) / 10
    y_hi = min(1.0, math.ceil((max(ys) + 0.05) * 10) / 10)
    yt = [round(y_lo + i * 0.1, 10) for i in range(int(round((y_hi - y_lo) * 10)) + 1)]
    plot_h = 34.0 * (len(yt) - 1)

    body: list[str] = []
    body.append(f'<text class="ctitle" x="0" y="18">{escape(title)}</text>')
    sub = (
        "Sections exactly right against classification cost per 1,000 packets, "
        "one point per approach and model"
    )
    body.append(f'<text class="sub" x="0" y="40">{escape(sub)}</text>')
    shown = [a for a in data.approaches if any(p.approach == a for p in every)]
    body.append(legend(shown, 66))
    y = 84.0
    y_title = axis_title(ACCURACY, metrics)
    desc: list[str] = []
    for n, ds in enumerate(panels):
        pts = pts_by[ds]
        name = PANEL_NAMES.get(ds) or chart_name(ds, data.reg["datasets"])
        size = data.sample(ds, [p.value for p in pts])
        header = f"{name}, {size}" if size else name
        body.append(
            f'<text class="ptitle" x="0" y="{y + 14:.1f}">{escape(header)}</text>'
        )
        body.append(
            f'<text class="axislabel" x="{left - 4}" y="{y + 32:.1f}">{escape(y_title)}</text>'
        )
        y0 = y + 44
        y1 = y0 + plot_h

        def py(v: float, y0: float = y0, y1: float = y1) -> float:
            return y1 - (v - yt[0]) / (yt[-1] - yt[0]) * (y1 - y0)

        for s in segs:
            for t in yt:
                body.append(
                    f'<line class="grid" x1="{s.x0:.1f}" x2="{s.x1:.1f}" '
                    f'y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
                )
            body.append(
                f'<line class="axis" x1="{s.x0:.1f}" x2="{s.x1:.1f}" y1="{y1:.1f}" y2="{y1:.1f}"/>'
                f'<line class="axis" x1="{s.x0:.1f}" x2="{s.x0:.1f}" y1="{y0:.1f}" y2="{y1:.1f}"/>'
            )
            for t in s.ticks:
                body.append(
                    f'<line class="axis" x1="{s.px(t):.1f}" x2="{s.px(t):.1f}" '
                    f'y1="{y1:.1f}" y2="{y1 + 4:.1f}"/>'
                    f'<text class="tick" x="{s.px(t):.1f}" y="{y1 + 16:.1f}" '
                    f'text-anchor="middle">{money_tick(t)}</text>'
                )
        for t in yt:
            body.append(
                f'<text class="tick" x="{left - 7}" y="{py(t) + 4:.1f}" '
                f'text-anchor="end">{pct(t)}</text>'
            )
        if len(segs) > 1:
            # The cost axis jumps: two slanted strokes in the gap between its stretches.
            mx = (segs[0].x1 + segs[1].x0) / 2
            body.append(
                f'<line class="axis" x1="{mx - 6:.1f}" y1="{y1 + 5:.1f}" x2="{mx - 2:.1f}" y2="{y1 - 5:.1f}"/>'
                f'<line class="axis" x1="{mx + 2:.1f}" y1="{y1 + 5:.1f}" x2="{mx + 6:.1f}" y2="{y1 - 5:.1f}"/>'
            )
        bounds_y = (y0 - 4, y1 - 2)
        for s in segs:
            seg_pts = [p for p in pts if s.lo <= p.x["mean"] <= s.hi]
            bounds = (s.x0 + 2, bounds_y[0], s.x1, bounds_y[1])
            if not seg_pts:
                # A stretch of the axis with nothing measured on this dataset yet.
                names = [
                    m
                    for m in data.models
                    if any(p.model == m and s.lo <= p.x["mean"] <= s.hi for p in every)
                ]
                text = f"{join(names)} still running" if names else "Still running"
                body.append(
                    f'<text class="none" x="{(s.x0 + s.x1) / 2:.1f}" '
                    f'y="{(y0 + y1) / 2:.1f}" text-anchor="middle">{escape(text)}</text>'
                )
                continue
            blocked: list[Rect] = []
            bands, dots = [], []
            centre: dict[str, tuple[float, float]] = {}
            noted: tuple[float, float, float, float] | None = None
            for p in seg_pts:
                cx, cy = s.px(p.x["mean"]), py(p.y["mean"])
                centre[p.value] = (cx, cy)
                ringed = n == 0 and p.value == start
                dy = 2 * p.y.get("se", 0)
                top, bot = py(p.y["mean"] + dy), py(p.y["mean"] - dy)
                if ringed:
                    noted = (cx - 8, min(top, cy - 8), cx + 8, max(bot, cy + 8))
                if bot - top > 2:
                    bands.append(
                        f'<rect class="band ap-{p.approach.hue}" x="{cx - 5:.1f}" '
                        f'y="{top:.1f}" width="10" height="{bot - top:.1f}" rx="5"/>'
                    )
                    blocked.append((cx - 6, top, cx + 6, bot))
                blocked.append((cx - 8, cy - 8, cx + 8, cy + 8))
                dots.append(
                    f"<g><title>{escape(p.approach.name)} with {escape(p.model)}: "
                    f"{pct(p.y['mean'])} of sections exactly right, "
                    f"{money(p.x['mean'])} per 1,000 packets</title>"
                    + mark(p.approach, cx, cy, extra=" ref" if ringed else "")
                    + "</g>"
                )
                desc.append(
                    f"{name}: {p.approach.name} with {p.model}, {pct(p.y['mean'])} of "
                    f"sections exactly right for {money(p.x['mean'])} per 1,000 packets."
                )
            body += bands + dots
            labels = []
            if noted is not None:
                lx, ly, anchor, r = place(
                    START_NOTE, noted, blocked, bounds, CHAR, True
                )
                blocked.append(r)
                labels.append(
                    f'<text class="plabel halo" x="{lx:.1f}" y="{ly:.1f}" '
                    f'text-anchor="{anchor}">{START_NOTE}</text>'
                )
            # One label per model, by its points: they sit apart by model.
            for m in data.models:
                mine = [p for p in seg_pts if p.model == m]
                if not mine:
                    continue
                xs = [centre[p.value][0] for p in mine]
                cys = [centre[p.value][1] for p in mine]
                box = (min(xs) - 8, min(cys) - 8, max(xs) + 8, max(cys) + 8)
                lx, ly, anchor, r = place(m, box, blocked, bounds)
                blocked.append(r)
                labels.append(
                    f'<text class="mlabel halo" x="{lx:.1f}" y="{ly:.1f}" '
                    f'text-anchor="{anchor}">{escape(m)}</text>'
                )
            body += labels
        y = y1 + 22 + (14 if n < len(panels) - 1 else 0)
    x_title = cost_axis_title(data.cost, metrics, "packets") + " — cheaper ←"
    body.append(
        f'<text class="axislabel" x="{(left + right) / 2:.1f}" y="{y + 12:.1f}" '
        f'text-anchor="middle">{escape(x_title)}</text>'
    )
    prices = price_caveat_svg(str(data.results.get("region") or ""))
    body.append(f'<text class="prices" x="0" y="{y + 34:.1f}">{escape(prices)}</text>')
    height = y + 42
    names = join(
        [DATASET_WORDS.get(ds) or chart_name(ds, data.reg["datasets"]) for ds in panels]
    )
    alt = (
        f"{title}: sections exactly right against classification cost per 1,000 "
        f"packets, one point per approach and model, on {names}"
    )
    return svg(W, height, title, " ".join(desc), body), alt


def tip(s: float) -> str:
    """A bar's value: 28%, or <1% for a share that rounds to nothing."""
    return "<1%" if 0 < s < 0.005 else pct(s)


def losses_chart(data: Data, ds: str) -> tuple[str, str] | None:
    """Where each approach loses sections: for each approach and model, the share of
    true sections lost to each kind of error, a column per kind on one scale. A kind
    no row reaches LOSS_FLOOR of gets a line under the chart instead; a model not
    analysed yet gets a muted line in its approach's group. Returns the SVG and its
    alt text; None when the dataset has no analysis."""
    rows = [
        (
            a,
            m,
            {k: v for k, _ in LOSS_KINDS if (v := data.loss(ds, a, m, k)) is not None},
        )
        for a in data.approaches
        for m in data.models
    ]
    rows = [(a, m, v) for a, m, v in rows if v]
    if not rows:
        return None
    worst = {k: max(v.get(k, 0) for _, _, v in rows) for k, _ in LOSS_KINDS}
    kinds = [(k, n) for k, n in LOSS_KINDS if worst[k] >= LOSS_FLOOR]
    rare = [(k, n) for k, n in LOSS_KINDS if worst[k] < LOSS_FLOOR]
    top = max(worst[k] for k, _ in kinds)
    scale = max(0.1, math.ceil(top * 10 - 1e-9) / 10)
    label_w = 190.0
    col_w = (W - label_w) / len(kinds)
    bar_max = col_w - 48
    row_h, group_h, bar_h = 20.0, 26.0, 12.0

    title = "Where each approach loses sections"
    n = data.analysed_n(ds)
    name = PANEL_NAMES.get(ds) or chart_name(ds, data.reg["datasets"])
    sub = f"Share of all true sections, by what went wrong · {name}" + (
        f", {n}" if n else ""
    )
    body = [
        f'<text class="ctitle" x="0" y="18">{escape(title)}</text>',
        f'<text class="sub" x="0" y="40">{escape(sub)}</text>',
    ]
    y = 68.0
    for i, (_, kname) in enumerate(kinds):
        x = label_w + i * col_w
        body.append(
            f'<text class="klabel" x="{x:.1f}" y="{y:.1f}">{escape(kname)}</text>'
        )
    y += 8
    desc = []
    for a in data.approaches:
        mine = [(m, v) for b, m, v in rows if b == a]
        if not mine:
            continue
        body.append(mark(a, 6, y + group_h / 2 + 2))
        body.append(
            f'<text class="gname" x="18" y="{y + group_h / 2 + 6:.1f}">{escape(a.name)}</text>'
        )
        y += group_h
        # Each column's baseline runs down the group's rows, clear of its name.
        for i in range(len(kinds)):
            x0 = label_w + i * col_w
            body.append(
                f'<line class="axis" x1="{x0:.1f}" x2="{x0:.1f}" y1="{y + 2:.1f}" '
                f'y2="{y + len(mine) * row_h - 2:.1f}"/>'
            )
        for m, v in mine:
            cy = y + row_h / 2
            body.append(
                f'<text class="label" x="18" y="{cy + 4:.1f}">{escape(m)}</text>'
            )
            for i, (k, kname) in enumerate(kinds):
                x0 = label_w + i * col_w
                s = v.get(k)
                if s is None:
                    continue
                w = s / scale * bar_max
                if s > 0:
                    w = max(w, 2.0)
                    r = min(4.0, w / 2)
                    body.append(
                        f'<path class="ap-{a.hue}" d="M{x0:.1f} {cy - bar_h / 2:.1f}'
                        f"H{x0 + w - r:.1f}A{r:.1f} {r:.1f} 0 0 1 {x0 + w:.1f} {cy - bar_h / 2 + r:.1f}"
                        f"V{cy + bar_h / 2 - r:.1f}A{r:.1f} {r:.1f} 0 0 1 {x0 + w - r:.1f} {cy + bar_h / 2:.1f}"
                        f'H{x0:.1f}Z"><title>{escape(a.name)} with {escape(m)}: '
                        f"{escape(tip(s))} {escape(kname.lower())}</title></path>"
                    )
                body.append(
                    f'<text class="val" x="{x0 + w + 4:.1f}" y="{cy + 4:.1f}">'
                    f"{escape(tip(s))}</text>"
                )
                desc.append(f"{a.name} with {m}: {tip(s)} {kname.lower()}.")
            y += row_h
        waiting = [m for m in data.models if m not in {m for m, _ in mine}]
        if waiting:
            body.append(
                f'<text class="none" x="18" y="{y + row_h / 2 + 4:.1f}">'
                f"{escape(join(waiting))}: still running</text>"
            )
            y += row_h
        y += 6
    foot = f"Bars share one scale, 0 to {pct(scale)}."
    for k, kname in rare:
        foot += (
            f" {kname}: none for any approach."
            if worst[k] <= 0
            else f" {kname}: {tip(worst[k])} of sections at most, for any approach."
        )
    body.append(f'<text class="prices" x="0" y="{y + 10:.1f}">{escape(foot)}</text>')
    height = y + 20
    shown = join([kname.lower() for _, kname in kinds])
    alt = (
        f"{title}: for each approach and model, the share of true sections lost to "
        f"each kind of error ({shown}), on {DATASET_WORDS.get(ds, name)}"
    )
    return svg(W, height, title, " ".join(desc), body), alt


# ---------------------------------------------------------------------------------
# The tables, collapsed


def losses_table(data: Data, ds: str) -> list[str]:
    rows = []
    for a in data.approaches:
        for m in data.models:
            v = data.analysed(ds, a, m)
            if not v:
                continue
            cells = [
                pct(s) if (s := data.loss(ds, a, m, k)) is not None else EMPTY
                for k, _ in LOSS_KINDS
            ]
            pred = data.predicted_sections(ds, a, m)
            au = data.auroc(ds, a, m)
            none, total = data.without_confidence(ds, a, m)
            rows.append(
                [
                    f"{a.name} · {m}",
                    *cells,
                    f"{pred:.2f}" if pred is not None else EMPTY,
                    cell_text(au, "auroc") if au else EMPTY,
                    pct(none / total) if total else EMPTY,
                ]
            )
    if not rows:
        return []
    name = PANEL_NAMES.get(ds) or chart_name(ds, data.reg["datasets"])
    true = data.true_sections(ds)
    head = [
        "Approach · model",
        *(n for _, n in LOSS_KINDS),
        "Predicted sections per packet",
        "Confidence AUROC",
        "Sections with no confidence",
    ]
    out = [
        f"**{name}: where sections were lost**",
        "",
        "| " + " | ".join(head) + " |",
        "|" + "---|" * len(head),
        *("| " + " | ".join(r) + " |" for r in rows),
        "",
    ]
    n = data.analysed_n(ds)
    notes = [f"{n}: the packets every run scored" if n else ""]
    if true:
        notes.append(f"{true:.2f} true sections per packet")
    notes.append(
        "AUROC is shown over two runs or more, for the sections whose pages were right"
    )
    out += ["*" + "; ".join(x for x in notes if x) + ".*", ""]
    return out


def details(data: Data, econ_rest: list[str]) -> list[str]:
    rendered = render_ablation(data.ab, data.reg, data.results or None)
    body = list(rendered.details)
    text = "\n".join(body)
    for a in data.approaches:
        if a.row != a.name:
            text = text.replace(a.row, a.name)
    out = ["<details>", "<summary>All the numbers</summary>", ""]
    out += [line for line in econ_rest]
    while out and out[-1] == "":
        out.pop()
    out.append("")
    for ds in data.datasets:
        out += losses_table(data, ds)
    out += [text.rstrip(), "", "</details>", ""]
    return out


# ---------------------------------------------------------------------------------
# The section and the page


def section(data: Data) -> tuple[list[str], dict[str, str]]:
    """The region's lines and its charts (file name -> SVG)."""
    out = [f"## {HEADING}", "", f"![{EXPLAINER_ALT}]({EXPLAINER})", ""]
    charts: dict[str, str] = {}
    if not data.results:
        return out + ["**Answer:** Not yet measured.", ""], charts
    ds = data.datasets[0]
    pk = picks(data.points(ds))
    out += [f"**Answer:** {answer(data)}", ""]
    econ = render_econ(data.ab, data.reg, data.results, data.cells)
    econ_rest: list[str] = []
    if econ and econ[0].startswith("**The economics:**"):
        out += [econ[0], ""]
        econ_rest = econ[2:]
    running = missing_line(data)
    if running:
        out += [running, ""]
    panels = data.panels()
    if panels:
        title = (
            "The model matters more than the approach"
            if model_matters_more(data.points(panels[0]))
            else "Sections exactly right against classification cost"
        )
        chart, alt = headline_chart(data, panels, title, pk.start.value if pk else None)
        charts[HEADLINE_SVG] = chart
        caption = "*Bands show ±2 standard errors."
        every = [p.x["mean"] for d in panels for p in data.points(d)]
        upper = cost_split(every)
        if upper is not None:
            low = max(x for x in every if x < upper)
            high = nice_ticks(
                min(x for x in every if x >= upper) * 0.88, max(every) * 1.06, 4
            )[0]
            top = nice_ticks(0, low * 1.08, 4)[-1]
            caption += (
                f" The cost axis jumps from {money_tick(top)} to {money_tick(high)}."
            )
        out += [f"![{alt}]({CHARTS_FROM_PAGES}/{HEADLINE_SVG})", "", caption + "*", ""]
    loss = losses_chart(data, ds)
    if loss:
        chart, alt = loss
        charts[LOSSES_SVG] = chart
        n = data.analysed_n(ds)
        out += [f"![{alt}]({CHARTS_FROM_PAGES}/{LOSSES_SVG})", ""]
        if n:
            words = DATASET_WORDS.get(ds, chart_name(ds, data.reg["datasets"]))
            out += [
                f"*Measured on the {n.split(' packets')[0]} {words} every run scored, as are "
                "the error, sections-per-packet and confidence figures below.*",
                "",
            ]
    for a in data.approaches:
        out += subsection(data, a, pk)
    long_lines, long_charts = long_section(data)
    out += long_lines
    charts.update(long_charts)
    out += details(data, econ_rest)
    return out, charts


def splice(text: str, region: str, path: Path) -> str:
    """Put the region between its markers; the first time, in place of the explainer
    figure that opens the page."""
    begin, end = markers(REGION)
    block = f"{begin}\n\n{region}\n{end}"
    if begin in text and end in text:
        head, rest = text.split(begin, 1)
        _, tail = rest.split(end, 1)
        return f"{head}{block}{tail}"
    line = next(
        (
            ln
            for ln in text.splitlines()
            if ln.startswith("![") and ln.endswith(f"({EXPLAINER})")
        ),
        None,
    )
    if line is None:
        raise SystemExit(f"{path}: no {begin} / {end} markers and no explainer figure")
    return text.replace(line, block, 1)


def load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def build(results_dir: Path, analysis_dir: Path) -> tuple[str, dict[str, str], Data]:
    reg = load_registry()
    ab = next(
        (a for a in reg["ablations"] if a.get("feature") and a["page"] == PAGE), None
    )
    if ab is None:
        raise SystemExit(f"no `feature` ablation on {PAGE}")
    if ab.get("feature") != HEADING:
        raise SystemExit(f"{ab['id']}: feature should be {HEADING!r}, the heading here")
    analyses = {
        ds: an
        for ds in ab["datasets"]
        if (an := load(analysis_dir / f"{ab['id']}-analysis-{ds}.json"))
    }
    long_ab = next((a for a in reg["ablations"] if a["id"] == LONG_ID), None)
    data = Data(
        reg,
        ab,
        load(results_dir / f"{ab['id']}.json"),
        analyses,
        long_ab,
        load(results_dir / f"{LONG_ID}.json"),
        load(results_dir / BREAKEVEN),
    )
    lines, charts = section(data)
    region = fix_id_links("\n".join(lines).rstrip() + "\n", PAGE, reg)
    return region, charts, data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if out of date")
    parser.add_argument(
        "--out", type=Path, help="write a copy of the page here instead"
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=RESULTS,
        help="where SC-13.json and SC-14.json are",
    )
    parser.add_argument(
        "--analysis-dir",
        type=Path,
        help="where the SC-13-analysis-<dataset>.json files are (default: --results-dir)",
    )
    args = parser.parse_args()
    region, charts, _ = build(args.results_dir, args.analysis_dir or args.results_dir)
    path = PAGES / f"{PAGE}.md"
    text = path.read_text(encoding="utf-8")
    updated = splice(text, region, path)
    if args.out:
        page_out = args.out / path.relative_to(REPO)
        charts_out = args.out / CHARTS.relative_to(REPO)
        page_out.parent.mkdir(parents=True, exist_ok=True)
        charts_out.mkdir(parents=True, exist_ok=True)
        page_out.write_text(updated, encoding="utf-8")
        (args.out / "approaches.md").write_text(region, encoding="utf-8")
        for name, body in charts.items():
            (charts_out / name).write_text(body, encoding="utf-8")
        print(
            f"wrote {page_out}, {args.out / 'approaches.md'} and {len(charts)} charts"
        )
        return 0
    stale = []
    if updated != text:
        stale.append(str(path.relative_to(REPO)))
        if not args.check:
            path.write_text(updated, encoding="utf-8")
            print(f"rendered {path.relative_to(REPO)}")
    existing = {p.name for p in CHARTS.glob("*.svg")} if CHARTS.exists() else set()
    for name, body in charts.items():
        target = CHARTS / name
        if not target.exists() or target.read_text(encoding="utf-8") != body:
            stale.append(str(target.relative_to(REPO)))
            if not args.check:
                CHARTS.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
                print(f"drew {target.relative_to(REPO)}")
    for name in sorted(existing - charts.keys()):
        stale.append(f"{(CHARTS / name).relative_to(REPO)} (no longer drawn)")
        if not args.check:
            (CHARTS / name).unlink()
            print(f"removed {(CHARTS / name).relative_to(REPO)}")
    if args.check and stale:
        print(
            "out of date, run render_splitting_approaches.py: " + ", ".join(stale),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
