#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Break a splitting-and-classification ablation's section accuracy down by cause.

A packet is a PDF holding several documents back to back. Classification splits it into
sections (page ranges) and gives each one a document class. Test Studio scores each
packet against its ground truth and keeps the details in the packet's evaluation
(`doc_split_metrics`): every true section with its pages and class, every predicted
section, and every page's predicted class and confidence. This script reads those
evaluations for every run of an ablation's manifest and reports, per value, why
sections were right or wrong.

Words used below:

    true section       one document of the ground truth: a class and a set of pages
    predicted section  one section the run returned: a class and a set of pages
    adjacent           two true sections next to each other in page order

Which packets are analysed (`--scope`):

    common (default)   the packets every run of every value scored, the rule the
                       collector uses for its per-document figures. A packet is scored
                       when it is in the run's `weightedOverallScores`. N is printed.
    scored             each run on every packet it scored, as Test Studio's own run
                       figures are; values then differ slightly in which packets count.

Every metric is computed per run and reported as the mean and standard error across the
value's runs (SE = stdev / sqrt(runs)), with the per-run figures beside it.

section_accuracy
    The share of a packet's true sections that some predicted section matches exactly:
    the same set of pages and the same class. It is each packet's own
    `split_accuracy_without_order`, averaged over the run's packets, so every packet
    weighs the same. Test Studio's run figure pools sections across packets instead, so
    a packet with more sections weighs more there; `sanity_check` shows both.

section_accuracy_by_length
    The same, for the packets in each of three page-count buckets. The edges are chosen
    from the analysed packets' page counts so that the buckets hold about the same
    number of packets, ties broken by how evenly they split every packet any run scored
    and then by rounder edges (multiples of 5, then even numbers). `--length-edges A,B`
    sets them instead: the buckets are then up to A pages, A+1 to B, and above B.

same_class_adjacent
    Packets whose ground truth has two adjacent true sections of the same class (two
    invoices in a row, say), against packets with none: section accuracy in each group.
    `pairs_merged` is the share of those same-class pairs whose boundary the run missed:
    the last page of the first and the first page of the second are in one predicted
    section. `different_class_pairs_merged` is the same share for adjacent pairs of
    different classes, the baseline to read it against.

errors
    Every true section gets exactly one category, tested in this order:

        correct      a predicted section has exactly its pages and its class
        wrong_class  a predicted section has exactly its pages, but another class
        merged       all of its pages are in one predicted section, and that predicted
                     section also holds pages of another true section
        split        its pages are spread over two or more predicted sections
        other        anything else: no predicted section holds any of its pages, some
                     of its pages are in no predicted section while the rest are in one,
                     or one predicted section holds all of its pages plus pages that
                     belong to no other true section

    The shares pool every true section of a run's packets, so they sum to 1 for each
    run. `split_kind` counts the split sections two ways: `within`, where every
    predicted section involved lies inside the true section (one document cut into
    pieces), and `across`, where at least one also takes pages of a neighbour (a
    boundary in the wrong place).

top_confusions
    The most frequent (true class -> predicted class) pairs among wrong_class sections,
    counted over every run of the value.

confidence
    For each true section that a predicted section matches on pages (correct or
    wrong_class), the section's confidence is the mean `predicted_confidence` of its
    pages. Pages with no confidence are skipped; a section with none at all is counted
    in `n_without_confidence` and left out. A section is right when its class is right.

        auroc  the chance that a random right section has a higher confidence than a
               random wrong one, ties counting half (the Mann-Whitney statistic).
               0.5 is no better than chance, 1.0 separates them perfectly.
        ece    expected calibration error: sections binned into ten equal-width
               confidence bins on [0, 1]; the mean over bins, weighted by sections, of
               |share right - mean confidence|. 0 is perfectly calibrated.

    Both are given per run (mean and SE over the runs where they are defined) and pooled
    over every run of the value, because a run has few wrong sections. When every
    section confidence of a value is the same number, `constant` is true and no AUROC is
    reported: it would be 0.5 by construction, not a measurement. `notes` says when an
    AUROC is undefined in some runs, and flags a value with 90% or more of its sections
    at one confidence, whose AUROC rests on the few sections that differ.

packets and predicted_sections_per_packet
    Pages and true sections per packet (min, median, max) over the analysed packets, and
    how many sections each value predicted per packet.

sanity_check
    Per value, the collector's `section_accuracy` cell (from `<ID>.json`) beside the
    same figure recomputed here: pooled over every packet each run scored, over the runs
    whose result reports one (this reproduces the cell), the per-packet mean over those
    packets, and the analysed figure. Pooling and the packet subset are the two
    differences between the cell and the analysed figure.

Evaluations are read from the collector's cache (`~/bench-work/guidance-runs/doceval`); a run
with any packet missing there is fetched from the stack's output bucket by the
collector's `DocEvals`, read-only. The packet lists come from the run results the
collector reads (`<results dir>/<run ID>-result.json`).

Usage:
    AWS_PROFILE=... python3 benchmarks/harness/analyze_splitting_approaches.py \\
        benchmarks/results/guidance/manifests/SC-11.json --dataset docsplit \\
        --out benchmarks/results/guidance/SC-11-analysis.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from collect_guidance_results import (
    DOC_EVAL_CACHE,
    OUT,
    REPO,
    DocEvals,
    mean_se,
    stack_resources,
)
from render_guidance import load_registry

#: Where the collector's run results (`idp-cli test-result --output-dir`) live.
RESULTS_DIR = (
    Path(os.environ.get("GUIDANCE_WORKDIR", "~/bench-work/guidance-runs")).expanduser()
    / "results"
)
CATEGORIES = ("correct", "wrong_class", "merged", "split", "other")
TOP_CONFUSIONS = 5
ECE_BINS = 10
#: A value whose section confidences sit at one number this often is flagged: its
#: AUROC rests on the few sections that differ.
NEARLY_CONSTANT = 0.9


@dataclass(frozen=True)
class Section:
    """A section: its document class and its set of page indices (0-based)."""

    cls: str
    pages: frozenset[int]

    @property
    def first(self) -> int:
        return min(self.pages) if self.pages else -1

    @property
    def last(self) -> int:
        return max(self.pages) if self.pages else -1


@dataclass
class Packet:
    """One packet's evaluation in one run, as far as this script reads it."""

    name: str
    total_pages: int
    truth: list[Section]  # in page order
    predicted: list[Section]
    confidence: dict[int, float | None]
    section_accuracy: float
    matched: int  # true sections the evaluation itself marks matched


@dataclass
class PacketScore:
    """What one packet contributes to its run's metrics."""

    section_accuracy: float
    categories: list[str]
    split_kinds: list[str]
    confusions: list[tuple[str, str]]
    confidences: list[tuple[float | None, bool]]
    same_pairs: int
    same_merged: int
    diff_pairs: int
    diff_merged: int
    predicted_sections: int
    agrees: bool  # our correct count equals the evaluation's own


def class_name(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("type") or "")
    return str(raw or "")


def parse_packet(name: str, doc: dict[str, Any]) -> Packet | None:
    """A packet's evaluation, or None when it has no section details."""
    split = doc.get("doc_split_metrics") or {}
    details = split.get("section_details_without_order") or []
    if not details:
        return None
    truth = sorted(
        (
            Section(
                class_name(d.get("ground_truth_class")),
                frozenset(int(p) for p in d.get("ground_truth_pages") or []),
            )
            for d in details
        ),
        key=lambda s: (s.first, s.last, s.cls),
    )
    predicted = [
        Section(
            class_name(s.get("document_class")),
            frozenset(int(p) for p in s.get("page_indices") or []),
        )
        for s in split.get("predicted_sections") or []
    ]
    confidence: dict[int, float | None] = {}
    for page in split.get("page_details") or []:
        value = page.get("predicted_confidence")
        confidence[int(page["page_index"])] = None if value is None else float(value)
    truth_pages = set().union(*(s.pages for s in truth))
    return Packet(
        name=name,
        total_pages=int(split.get("total_pages") or len(truth_pages)),
        truth=truth,
        predicted=predicted,
        confidence=confidence,
        section_accuracy=float(split.get("split_accuracy_without_order") or 0.0),
        matched=sum(bool(d.get("matched")) for d in details),
    )


def categorize(index: int, packet: Packet) -> tuple[str, str | None]:
    """A true section's error category (see the module docstring), and for a split
    section whether it stays `within` the true section or reaches `across` it."""
    t = packet.truth[index]
    exact = [s for s in packet.predicted if t.pages and s.pages == t.pages]
    if any(s.cls == t.cls for s in exact):
        return "correct", None
    if exact:
        return "wrong_class", None
    holding = [s for s in packet.predicted if s.pages & t.pages]
    if len(holding) == 1 and t.pages <= holding[0].pages:
        others = (u for i, u in enumerate(packet.truth) if i != index and u.pages)
        if any(u.pages & holding[0].pages for u in others):
            return "merged", None
        return "other", None
    if len(holding) >= 2:
        within = all(s.pages <= t.pages for s in holding)
        return "split", "within" if within else "across"
    return "other", None


def section_confidence(t: Section, packet: Packet) -> float | None:
    values = [packet.confidence.get(p) for p in t.pages]
    known = [v for v in values if v is not None]
    return statistics.fmean(known) if known else None


def boundary_missed(a: Section, b: Section, predicted: list[Section]) -> bool:
    """Whether the last page of `a` and the first page of `b` share a predicted
    section: the boundary between the two was not found."""
    return any(a.last in s.pages and b.first in s.pages for s in predicted)


def score_packet(packet: Packet) -> PacketScore:
    categories: list[str] = []
    split_kinds: list[str] = []
    confusions: list[tuple[str, str]] = []
    confidences: list[tuple[float | None, bool]] = []
    for i, t in enumerate(packet.truth):
        category, kind = categorize(i, packet)
        categories.append(category)
        if kind:
            split_kinds.append(kind)
        if category in ("correct", "wrong_class"):
            confidences.append((section_confidence(t, packet), category == "correct"))
        if category == "wrong_class":
            predicted = next(s.cls for s in packet.predicted if s.pages == t.pages)
            confusions.append((t.cls, predicted))
    same_pairs = same_merged = diff_pairs = diff_merged = 0
    for a, b in pairwise(packet.truth):
        if not a.pages or not b.pages:
            continue
        missed = boundary_missed(a, b, packet.predicted)
        if a.cls == b.cls:
            same_pairs += 1
            same_merged += missed
        else:
            diff_pairs += 1
            diff_merged += missed
    n_true = len(packet.truth)
    return PacketScore(
        section_accuracy=packet.section_accuracy,
        categories=categories,
        split_kinds=split_kinds,
        confusions=confusions,
        confidences=confidences,
        same_pairs=same_pairs,
        same_merged=same_merged,
        diff_pairs=diff_pairs,
        diff_merged=diff_merged,
        predicted_sections=len(packet.predicted),
        agrees=categories.count("correct") == packet.matched
        and abs(categories.count("correct") / n_true - packet.section_accuracy) < 1e-9,
    )


def auroc(right: list[float], wrong: list[float]) -> float | None:
    """Mann-Whitney AUROC: P(right > wrong) + 0.5 P(right == wrong)."""
    if not right or not wrong:
        return None
    wins = sum((r > w) + 0.5 * (r == w) for r in right for w in wrong)
    return wins / (len(right) * len(wrong))


def ece(pairs: list[tuple[float, bool]]) -> float | None:
    """Expected calibration error over ECE_BINS equal-width bins on [0, 1]."""
    if not pairs:
        return None
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(ECE_BINS)]
    for conf, right in pairs:
        index = min(max(int(conf * ECE_BINS), 0), ECE_BINS - 1)
        bins[index].append((conf, right))
    return sum(
        len(b)
        / len(pairs)
        * abs(statistics.fmean(r for _, r in b) - statistics.fmean(c for c, _ in b))
        for b in bins
        if b
    )


def across_runs(per_run: Sequence[float | None]) -> dict[str, Any] | None:
    """Mean and SE over the runs where a figure is defined, with the per-run list."""
    known = [v for v in per_run if v is not None]
    if not known:
        return None
    return {
        **mean_se(known),
        "runs": len(known),
        "per_run": [None if v is None else round(v, 6) for v in per_run],
    }


def summary(values: list[int]) -> dict[str, float]:
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def bucket_of(pages: int, edges: tuple[int, int]) -> int:
    return 0 if pages <= edges[0] else 1 if pages <= edges[1] else 2


def imbalance(lengths: list[int], edges: tuple[int, int]) -> float:
    counts = Counter(bucket_of(n, edges) for n in lengths)
    return max(abs(counts[b] - len(lengths) / 3) for b in range(3))


def roundness(edge: int) -> int:
    return 2 if edge % 5 == 0 else 1 if edge % 2 == 0 else 0


def choose_edges(lengths: list[int], universe: list[int]) -> tuple[int, int]:
    """Two upper edges splitting `lengths` into three buckets of about equal size."""
    lo, hi = min(lengths), max(lengths)
    candidates = [(a, b) for a in range(lo, hi) for b in range(a + 1, hi)]
    if not candidates:
        raise SystemExit("the analysed packets are too alike in length to bucket")
    return min(
        candidates,
        key=lambda e: (
            imbalance(lengths, e),
            imbalance(universe, e),
            -(roundness(e[0]) + roundness(e[1])),
            e,
        ),
    )


def bucket_labels(edges: tuple[int, int], lo: int, hi: int) -> list[str]:
    a, b = edges
    return [f"{lo}-{a} pages", f"{a + 1}-{b} pages", f"{b + 1}-{max(hi, b + 1)} pages"]


def describe(packets: list[Packet]) -> dict[str, Any]:
    """The ground truth of a set of packets, one evaluation per packet."""
    pages = [p.total_pages for p in packets]
    sections = [len(p.truth) for p in packets]
    same_adjacent = [
        p.name for p in packets if any(a.cls == b.cls for a, b in pairwise(p.truth))
    ]
    repeated = [p.name for p in packets if len({s.cls for s in p.truth}) < len(p.truth)]
    return {
        "packets": len(packets),
        "pages": sum(pages),
        "true_sections": sum(sections),
        "pages_per_packet": summary(pages),
        "sections_per_packet": summary(sections),
        "pages_distribution": dict(sorted(Counter(pages).items())),
        "packets_with_same_class_adjacent": same_adjacent,
        "packets_repeating_a_class": repeated,
        "true_sections_without_pages": sum(
            not s.pages for p in packets for s in p.truth
        ),
    }


class Evaluations:
    """Per-packet evaluations: from the collector's cache, else from S3 through the
    collector's DocEvals. The stack is looked up only when something is missing."""

    def __init__(self, stack: str, region: str, cache: Path) -> None:
        self.stack = stack
        self.region = region
        self.cache = cache
        self.remote: DocEvals | None = None

    def path(self, run_id: str, doc: str) -> Path:
        return self.cache / run_id / doc / "evaluation" / "results.json"

    def get(self, run_id: str, docs: list[str]) -> dict[str, dict[str, Any]]:
        if all(self.path(run_id, d).exists() for d in docs):
            return {
                d: json.loads(self.path(run_id, d).read_text(encoding="utf-8"))
                for d in docs
            }
        if self.remote is None:
            # Read-only: ListStackResources, then GetObject per missing packet.
            resources = stack_resources(self.stack, self.region)
            self.remote = DocEvals(resources, self.region, self.cache)
        return self.remote.evaluations(run_id, docs)


def run_figures(scores: list[PacketScore], buckets: list[int]) -> dict[str, Any]:
    """One run's metrics over its analysed packets (`buckets` aligned to `scores`)."""
    categories = Counter(c for s in scores for c in s.categories)
    n_true = sum(categories.values())
    with_same = [s.section_accuracy for s in scores if s.same_pairs]
    without_same = [s.section_accuracy for s in scores if not s.same_pairs]
    same_pairs = sum(s.same_pairs for s in scores)
    diff_pairs = sum(s.diff_pairs for s in scores)
    return {
        "section_accuracy": statistics.fmean(s.section_accuracy for s in scores),
        "by_length": [
            statistics.fmean(vals) if vals else None
            for vals in (
                [
                    s.section_accuracy
                    for s, b in zip(scores, buckets, strict=True)
                    if b == k
                ]
                for k in range(3)
            )
        ],
        "by_length_packets": [buckets.count(k) for k in range(3)],
        "with_same": statistics.fmean(with_same) if with_same else None,
        "without_same": statistics.fmean(without_same) if without_same else None,
        "packets_with_same": len(with_same),
        "same_pairs": same_pairs,
        "same_merged": (
            sum(s.same_merged for s in scores) / same_pairs if same_pairs else None
        ),
        "diff_pairs": diff_pairs,
        "diff_merged": (
            sum(s.diff_merged for s in scores) / diff_pairs if diff_pairs else None
        ),
        "n_true": n_true,
        "shares": {c: categories[c] / n_true for c in CATEGORIES},
        "counts": {c: categories[c] for c in CATEGORIES},
        "split_kinds": Counter(k for s in scores for k in s.split_kinds),
        "confusions": Counter(c for s in scores for c in s.confusions),
        "confidences": [c for s in scores for c in s.confidences],
        "predicted_per_packet": statistics.fmean(s.predicted_sections for s in scores),
        "disagreements": sum(not s.agrees for s in scores),
    }


def confidence_figures(runs: list[list[tuple[float | None, bool]]]) -> dict[str, Any]:
    """The confidence block of one value, from each run's (confidence, right) pairs."""
    scored = [[(c, ok) for c, ok in run if c is not None] for run in runs]
    pooled = [pair for run in scored for pair in run]
    n_right = sum(ok for _, ok in pooled)
    distinct = Counter(round(c, 6) for c, _ in pooled)
    out: dict[str, Any] = {
        "n_right": n_right,
        "n_wrong": len(pooled) - n_right,
        "n_without_confidence": sum(c is None for run in runs for c, _ in run),
        "per_run_right_wrong": [
            [sum(ok for _, ok in run), sum(not ok for _, ok in run)] for run in scored
        ],
        "distinct_values": len(distinct),
        "share_at_most_common": (
            round(distinct.most_common(1)[0][1] / len(pooled), 6) if pooled else None
        ),
        "constant": len(distinct) == 1,
    }

    def run_auroc(run: list[tuple[float, bool]]) -> float | None:
        if len({c for c, _ in run}) <= 1:
            return None
        return auroc([c for c, ok in run if ok], [c for c, ok in run if not ok])

    notes: list[str] = []
    out["notes"] = notes
    if out["n_without_confidence"]:
        notes.append(f"{out['n_without_confidence']} sections carry no confidence")
    if not pooled:
        out.update(auroc=None, ece=None, pooled={"auroc": None, "ece": None})
        notes.insert(0, "no section with matched pages carries a confidence")
        return out
    out["ece"] = across_runs([ece(run) for run in scored])
    pooled_ece = ece(pooled)
    if out["constant"]:
        out["auroc"] = None
        out["pooled"] = {"auroc": None, "ece": round(pooled_ece or 0.0, 6)}
        notes.insert(
            0,
            f"every section confidence is {next(iter(distinct))}: AUROC not defined",
        )
        return out
    out["auroc"] = across_runs([run_auroc(run) for run in scored])
    pooled_auroc = run_auroc(pooled)
    out["pooled"] = {
        "auroc": None if pooled_auroc is None else round(pooled_auroc, 6),
        "ece": None if pooled_ece is None else round(pooled_ece, 6),
    }
    if out["n_wrong"] == 0:
        notes.insert(0, "no wrong-class section carries a confidence: AUROC undefined")
    elif out["auroc"] is None or out["auroc"]["runs"] < len(runs):
        undefined = len(runs) - (out["auroc"]["runs"] if out["auroc"] else 0)
        notes.insert(
            0,
            f"AUROC undefined in {undefined} of {len(runs)} runs "
            "(no wrong-class section, or one confidence value)",
        )
    share = out["share_at_most_common"]
    if share >= NEARLY_CONSTANT:
        mode = distinct.most_common(1)[0][0]
        notes.append(
            f"nearly constant: {share:.1%} of sections at {mode}, "
            "so AUROC rests on the few that differ"
        )
    return out


def fmt(cell: dict[str, Any] | None, digits: int = 2) -> str:
    if cell is None:
        return "n/a"
    return f"{cell['mean']:.{digits}f}±{cell['se']:.{digits}f}"


def num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def table(title: str, header: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    print(f"\n{title}")
    for r in [header, *rows]:
        print(
            "  "
            + "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip()
        )


def short_name(value: str, shorts: dict[str, str]) -> str:
    return shorts.get(
        value, " · ".join(shorts.get(part, part) for part in value.split(" · "))
    )


def explain(packet: Packet) -> None:
    """Print a packet's true sections with their categories, for checking by hand."""
    print(f"\n{packet.name}: {packet.total_pages} pages")
    for i, t in enumerate(packet.truth):
        category, kind = categorize(i, packet)
        pages = f"{t.first}-{t.last}" if t.pages else "none"
        label = category + (f" ({kind})" if kind else "")
        print(f"  true {t.cls:<24} pages {pages:<7} -> {label}")
    for s in sorted(packet.predicted, key=lambda s: (s.first, s.last)):
        print(f"  pred {s.cls:<24} pages {s.first}-{s.last}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--dataset", required=True, help="dataset key in the manifest")
    parser.add_argument("--out", type=Path, help="write the analysis JSON here")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--doceval-cache", type=Path, default=DOC_EVAL_CACHE)
    parser.add_argument("--scope", choices=("common", "scored"), default="common")
    parser.add_argument(
        "--length-edges", help="two upper bucket edges in pages, e.g. 12,18"
    )
    parser.add_argument(
        "--cells",
        type=Path,
        help="the collector's results file (default: results/guidance/<ID>.json)",
    )
    parser.add_argument(
        "--explain",
        action="append",
        default=[],
        metavar="RUN_ID/PACKET",
        help="print one packet's sections with their categories",
    )
    args = parser.parse_args()

    man = json.loads(args.manifest.read_text(encoding="utf-8"))
    runs_by_value: dict[str, list[str]] = (man.get("runs") or {}).get(
        args.dataset
    ) or {}
    if not runs_by_value:
        raise SystemExit(f"{args.manifest}: no runs for dataset {args.dataset!r}")
    entry: dict[str, Any] = next(
        (
            a
            for a in load_registry().get("ablations") or []
            if a.get("id") == man["ablation"]
        ),
        {},
    )
    order = [v for v in entry.get("values") or [] if v in runs_by_value]
    order += [v for v in runs_by_value if v not in order]
    shorts: dict[str, str] = entry.get("short_values") or {}

    evaluations = Evaluations(man["stack"], man["region"], args.doceval_cache)
    packets: dict[str, dict[str, Packet]] = {}  # run ID -> packet -> evaluation
    run_level: dict[str, float | None] = {}  # Test Studio's own section accuracy
    warnings: list[str] = []
    for value in order:
        for run_id in runs_by_value[value]:
            path = args.results_dir / f"{run_id}-result.json"
            if not path.exists():
                raise SystemExit(
                    f"{path}: missing; write it with "
                    f"`idp-cli test-result {run_id} --output-dir {args.results_dir}`"
                )
            result = json.loads(path.read_text(encoding="utf-8"))
            split = result.get("splitClassificationMetrics") or {}
            figure = split.get("split_accuracy_without_order")
            run_level[run_id] = None if figure is None else float(figure)
            names = sorted(
                {k.split("/", 1)[1] for k in result.get("weightedOverallScores") or {}}
            )
            found = evaluations.get(run_id, names)
            packets[run_id] = {}
            for name in names:
                parsed = parse_packet(name, found.get(name) or {})
                if parsed is None:
                    warnings.append(f"{run_id}/{name}: no section details; left out")
                else:
                    packets[run_id][name] = parsed

    for target in args.explain:
        run_id, _, name = target.partition("/")
        if name not in packets.get(run_id, {}):
            raise SystemExit(f"--explain {target}: no such run and packet")
        explain(packets[run_id][name])

    all_runs = [r for v in order for r in runs_by_value[v]]
    universe_names = sorted(set().union(*(packets[r] for r in all_runs)))
    common = sorted(set.intersection(*(set(packets[r]) for r in all_runs)))
    # The ground truth of a packet is the same in every run; one evaluation each.
    reference: dict[str, Packet] = {}
    for run_id in all_runs:
        for name, packet in packets[run_id].items():
            first = reference.setdefault(name, packet)
            same = (first.total_pages, first.truth) == (
                packet.total_pages,
                packet.truth,
            )
            if not same:
                warnings.append(f"{run_id}/{name}: ground truth differs between runs")
    if args.scope == "common" and not common:
        raise SystemExit("no packet was scored by every run of every value")
    analysed_names = common if args.scope == "common" else universe_names
    universe = [reference[n] for n in universe_names]
    analysed = [reference[n] for n in analysed_names]

    universe_lengths = [p.total_pages for p in universe]
    if args.length_edges:
        a, b = (int(x) for x in args.length_edges.split(","))
        edges = (a, b)
    else:
        edges = choose_edges([p.total_pages for p in analysed], universe_lengths)
    labels = bucket_labels(edges, min(universe_lengths), max(universe_lengths))

    values_out: dict[str, Any] = {}
    sanity: dict[str, Any] = {}
    figures_by_value: dict[str, list[dict[str, Any]]] = {}
    for value in order:
        ids = runs_by_value[value]
        figures = []
        for run_id in ids:
            names = common if args.scope == "common" else sorted(packets[run_id])
            chosen = [packets[run_id][n] for n in names]
            figures.append(
                run_figures(
                    [score_packet(p) for p in chosen],
                    [bucket_of(reference[p.name].total_pages, edges) for p in chosen],
                )
            )
        figures_by_value[value] = figures
        split_kinds: Counter[str] = sum((f["split_kinds"] for f in figures), Counter())
        confusions: Counter[tuple[str, str]] = sum(
            (f["confusions"] for f in figures), Counter()
        )
        values_out[value] = {
            "runs": ids,
            "packets_per_run": [
                len(common) if args.scope == "common" else len(packets[r]) for r in ids
            ],
            "section_accuracy": across_runs([f["section_accuracy"] for f in figures]),
            "section_accuracy_by_length": {
                label: {
                    "cell": across_runs([f["by_length"][k] for f in figures]),
                    "packets_per_run": [f["by_length_packets"][k] for f in figures],
                }
                for k, label in enumerate(labels)
            },
            "same_class_adjacent": {
                "packets_with_per_run": [f["packets_with_same"] for f in figures],
                "section_accuracy_with": across_runs([f["with_same"] for f in figures]),
                "section_accuracy_without": across_runs(
                    [f["without_same"] for f in figures]
                ),
                "pairs_per_run": [f["same_pairs"] for f in figures],
                "pairs_merged": across_runs([f["same_merged"] for f in figures]),
                "different_class_pairs_per_run": [f["diff_pairs"] for f in figures],
                "different_class_pairs_merged": across_runs(
                    [f["diff_merged"] for f in figures]
                ),
            },
            "errors": {
                "true_sections_per_run": [f["n_true"] for f in figures],
                "shares": {
                    c: across_runs([f["shares"][c] for f in figures])
                    for c in CATEGORIES
                },
                "counts": {c: sum(f["counts"][c] for f in figures) for c in CATEGORIES},
                "split_kind": {k: split_kinds[k] for k in ("within", "across")},
            },
            "top_confusions": [
                {"true": t, "predicted": p, "count": n}
                for (t, p), n in confusions.most_common(TOP_CONFUSIONS)
            ],
            "confidence": confidence_figures([f["confidences"] for f in figures]),
            "predicted_sections_per_packet": across_runs(
                [f["predicted_per_packet"] for f in figures]
            ),
        }
        disagreements = sum(f["disagreements"] for f in figures)
        if disagreements:
            warnings.append(
                f"{value}: {disagreements} packet evaluations where the exact-match "
                "count here differs from the evaluation's own"
            )

        # The collector's cell, and the same figure rebuilt from the packets.
        pooled, macro = [], []
        for run_id in ids:
            scored = list(packets[run_id].values())
            correct = sum(p.matched for p in scored)
            pooled.append(correct / sum(len(p.truth) for p in scored))
            macro.append(statistics.fmean(p.section_accuracy for p in scored))
        reported = [i for i, r in enumerate(ids) if run_level[r] is not None]
        sanity[value] = {
            "pooled_all_scored": across_runs(pooled),
            "pooled_all_scored_reporting_runs": across_runs(
                [pooled[i] for i in reported]
            ),
            "packet_mean_all_scored": across_runs(macro),
            "analysed": values_out[value]["section_accuracy"],
            "runs_without_run_figure": [r for r in ids if run_level[r] is None],
        }

    cells_path = args.cells or OUT / f"{man['ablation']}.json"
    cells = (
        json.loads(cells_path.read_text(encoding="utf-8")).get("cells") or {}
        if cells_path.exists()
        else {}
    )
    for value, check in sanity.items():
        cell = cells.get(f"{args.dataset}|{value}|section_accuracy")
        check["cell"] = cell
        if cell:
            rebuilt = check["pooled_all_scored_reporting_runs"]["mean"]
            check["cell_reproduced"] = abs(rebuilt - cell["mean"]) < 1e-4
            check["analysed_minus_cell"] = round(
                check["analysed"]["mean"] - cell["mean"], 6
            )

    dropped = {
        name: [r for r in all_runs if name not in packets[r]]
        for name in universe_names
        if name not in common
    }
    out = {
        "ablation": man["ablation"],
        "dataset": args.dataset,
        "manifest": str(args.manifest.resolve().relative_to(REPO))
        if args.manifest.resolve().is_relative_to(REPO)
        else str(args.manifest),
        "release": man.get("release"),
        "measured": man.get("measured"),
        "scope": args.scope,
        "packets": {
            "analysed": analysed_names,
            "n": len(analysed_names),
            "scored_by_some_run": len(universe_names),
            "left_out": {n: len(r) for n, r in dropped.items()}
            if args.scope == "common"
            else {},
            "analysed_ground_truth": describe(analysed),
            "all_scored_ground_truth": describe(universe),
        },
        "length_buckets": {
            "edges": list(edges),
            "labels": labels,
            "chosen": "given" if args.length_edges else "automatic",
            "analysed_counts": [
                sum(bucket_of(p.total_pages, edges) == k for p in analysed)
                for k in range(3)
            ],
            "all_scored_counts": [
                sum(bucket_of(n, edges) == k for n in universe_lengths)
                for k in range(3)
            ],
        },
        "values": values_out,
        "sanity_check": {"cells": str(cells_path.name), "values": sanity},
        "warnings": warnings,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    report(out, order, shorts)
    if args.out:
        print(f"\nwrote {args.out}")
    return 0


def report(out: dict[str, Any], order: list[str], shorts: dict[str, str]) -> None:
    """The analysis as compact tables on stdout."""
    pk = out["packets"]
    truth = pk["analysed_ground_truth"]
    print(
        f"{out['ablation']} {out['dataset']}: N = {pk['n']} packets analysed "
        f"(scope {out['scope']}) of {pk['scored_by_some_run']} scored by some run"
    )
    print(f"  analysed: {', '.join(pk['analysed'])}")
    if pk["left_out"]:
        print(f"  left out (runs that did not score it): {pk['left_out']}")
    pp, sp = truth["pages_per_packet"], truth["sections_per_packet"]
    print(
        f"  pages per packet {pp['min']}/{pp['median']}/{pp['max']} (min/median/max), "
        f"true sections per packet {sp['min']}/{sp['median']}/{sp['max']}, "
        f"{truth['pages']} pages, {truth['true_sections']} true sections"
    )
    print(f"  page counts: {truth['pages_distribution']}")
    lb = out["length_buckets"]
    print(
        f"  length buckets {lb['labels']} ({lb['chosen']}): analysed "
        f"{lb['analysed_counts']}, all scored {lb['all_scored_counts']}"
    )
    print(
        "  packets with adjacent same-class sections: "
        f"{len(truth['packets_with_same_class_adjacent'])}; repeating a class "
        f"anywhere: {len(truth['packets_repeating_a_class'])}"
    )
    values = out["values"]
    names = {v: short_name(v, shorts) for v in order}

    table(
        "Section accuracy, overall and by packet length (mean±SE over runs)",
        ["value", "all", *lb["labels"]],
        [
            [
                names[v],
                fmt(values[v]["section_accuracy"]),
                *(
                    fmt(values[v]["section_accuracy_by_length"][label]["cell"])
                    for label in lb["labels"]
                ),
            ]
            for v in order
        ],
    )
    table(
        "Same class back to back: section accuracy by group, share of pairs merged",
        ["value", "with", "without", "same-class merged", "other pairs merged"],
        [
            [
                names[v],
                fmt(s["section_accuracy_with"]),
                fmt(s["section_accuracy_without"]),
                fmt(s["pairs_merged"]),
                fmt(s["different_class_pairs_merged"]),
            ]
            for v in order
            for s in [values[v]["same_class_adjacent"]]
        ],
    )
    table(
        "Error breakdown over true sections (share, mean±SE over runs)",
        ["value", *CATEGORIES, "split within/across"],
        [
            [
                names[v],
                *(fmt(e["shares"][c]) for c in CATEGORIES),
                f"{e['split_kind']['within']}/{e['split_kind']['across']}",
            ]
            for v in order
            for e in [values[v]["errors"]]
        ],
    )
    print("\nTop confusions among wrong_class sections (count over all runs)")
    for v in order:
        top = values[v]["top_confusions"]
        listed = ", ".join(f"{c['true']}->{c['predicted']} x{c['count']}" for c in top)
        print(f"  {names[v]}: {listed or 'none'}")
    table(
        "Classification confidence of sections with matched pages",
        [
            "value",
            "right",
            "wrong",
            "no conf",
            "AUROC runs",
            "AUROC pooled",
            "ECE pooled",
            "note",
        ],
        [
            [
                names[v],
                str(c["n_right"]),
                str(c["n_wrong"]),
                str(c["n_without_confidence"]),
                fmt(c["auroc"]) + (f" ({c['auroc']['runs']})" if c["auroc"] else ""),
                num(c["pooled"]["auroc"]),
                num(c["pooled"]["ece"]),
                "; ".join(
                    [*c["notes"], f"distinct confidence values: {c['distinct_values']}"]
                ),
            ]
            for v in order
            for c in [values[v]["confidence"]]
        ],
    )
    table(
        "Predicted sections per packet (true: "
        f"{truth['true_sections'] / truth['packets']:.2f})",
        ["value", "predicted"],
        [[names[v], fmt(values[v]["predicted_sections_per_packet"])] for v in order],
    )
    sanity = out["sanity_check"]["values"]
    table(
        f"Sanity check against {out['sanity_check']['cells']} section_accuracy",
        [
            "value",
            "cell",
            "rebuilt",
            "pooled all",
            "packet mean all",
            "analysed",
            "analysed-cell",
        ],
        [
            [
                names[v],
                fmt(s["cell"], 3),
                fmt(s["pooled_all_scored_reporting_runs"], 3)
                + ("" if s.get("cell_reproduced", True) else " MISMATCH"),
                fmt(s["pooled_all_scored"], 3),
                fmt(s["packet_mean_all_scored"], 3),
                fmt(s["analysed"], 3),
                num(s.get("analysed_minus_cell"), 3),
            ]
            for v in order
            for s in [sanity[v]]
        ],
    )
    for w in out["warnings"]:
        print(f"  warning: {w}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
