#!/usr/bin/env python3
"""Render the ablation tables of the "Benchmarks by Problem Set" pages.

Reads the ablation registry (benchmarks/matrices/guidance_ablations.yaml) and writes the
region of each page under docs/benchmark-guidance/ between

    <!-- BEGIN GENERATED FAQ --> and <!-- END GENERATED FAQ -->

and the dataset list of datasets.md between DATASETS markers. Text outside the markers is
hand-written and left alone. Each question gets its headline chart directly under its
heading, then its answer (the results file's `verdict`), then a collapsed
<details> block holding what was run and the results tables. When a results file exists
at benchmarks/results/guidance/<ID>.json, its values fill the entry; otherwise, or when
the ablation is `deprecated`, the question is listed, with its ID, under "Planned" at the
end of the page's FAQ. An ablation with a `baseline` says in its details that everything
else is that configuration (the recommended setup), and its `reference` value is bold in
every results table. A dataset where every value scores CEILING weighted accuracy or more
is not charted: one line says it is too easy to separate.
A results file looks like:

    {
      "release": "0.6.15",
      "measured": "December 2026",
      "region": "us-west-2",
      "source": "benchmarks/results/v0.6.15/<suite>/summary.json",
      "verdict": "Use agentic extraction above about 400 rows.",
      "cells": {"<dataset key>|<value>|<metric key>": {"mean": 0.812, "se": 0.006}},
      "models": {"<dataset key>|<value>": {"OCR": "...", "Extraction": "...", ...}},
      "sample": {"<dataset key>|<value>": {"docs": [20, 20], "failed": [0, 0], "runs": 2}},
      "usage": {"<dataset key>|<value>": [{"bedrock/<model id>|inputTokens": 41250, ...}]},
      "cost_basis": {"<dataset key>|<value>": {"mode": "cold-run", "index": 0}}
    }

A cell is either a string, printed as is, or a mean and standard error over the runs,
printed as "0.81 ± 0.01" (two standard errors). For each dataset kind, the ablation's
`plot` metric (or its first metric) is drawn as an SVG under images/benchmark-guidance/
whenever at least one of its cells has a mean: one panel per dataset, each point
labelled with the value's short name (`short_values`), titled with the ablation's
`takeaway` and subtitled with the dataset, the sample, what varies and the model held
fixed. `plot` may list several metrics: a scatter then draws a row of panels per
metric. A metric with a `baseline` in the registry gets it as a dashed line, and an
ablation marked `families` colors and shapes its points by model family.

Each FAQ entry carries one chart. An ablation with a `by_size` key gets a chart of one
per-document score against the document's own size, drawn from the results file's
`per_doc` block, which is that chart: a mean over a dataset says which value wins, and
this says at what size it starts winning. Its scatter goes to the details. An ablation
with a `grid` gets the grid's tables in its details in place of the results table.

`models` is optional: the model in force at each step of the pipeline. Each details
table states it in full, and a step whose model differs across the values reads
"varies (the setting under test)"; a chart names only the model held fixed.

`sample` is optional: the documents each run scored and the number of runs, which
every chart, caption and details table states ("20 × 3 runs", "N = 20 documents x 3
runs"). A document that failed was not scored, so the caption says how many were
left out.

`usage` is optional: each run's Bedrock and Textract usage per document. The renderer
prices it with config_library/pricing.yaml and adds a "Cost per 1,000 documents"
column to every table, plus the results file's `long_context` surcharge, which prices
the Claude Haiku 5.5 requests over 100,000 prompt tokens at the long-context rate the
price list's single rate per unit cannot hold (price_usage; the collector sizes the
requests). `cost_basis` says which runs to price: by default only the
coldest run of each value, because repeat runs read the prompt cache the first run
wrote. When every plotted point has a cost, the plot puts cost on the x-axis, and when
the plotted metric is an accuracy, it draws the marginal cost of error from the
cheapest value to each more accurate one (marginal-cost-of-error.md). An ablation
with a `cost_metric` (such as classification_cost_per_1000_docs, which the collector
writes) uses that cell instead of the whole pipeline's cost, everywhere a cost is
shown: the chart's x-axis, the tables' cost column, the cost per error prevented and
the cheapest value.

On a DocSplit packet, section accuracy (and label and boundary accuracy) is a share of
the packet's sections, so the cost per error prevented is per section: the results
file's `sections_per_doc` (`pages_per_doc` for page accuracy) says how many a packet
holds, and the price is divided by it.

An ablation with an `econ` gets its economics under the answer (render_econ): one
sentence on what `econ.line` costs and buys against `econ.against`, and a table of
every other value against `against`, each priced per error it avoids.

An ablation with a `feature` (the heading of a section another generator writes on its
page, outside the FAQ markers: render_splitting_approaches.py for SC-13) keeps its
question in the FAQ, so links to it still land, with one line linking to that section
in place of its entry (render_feature).

Usage:
    python3 benchmarks/harness/render_guidance.py           # rewrite the pages
    python3 benchmarks/harness/render_guidance.py --check   # exit 1 if a page is stale
    python3 benchmarks/harness/render_guidance.py --preview EX-02 \
        --results sample.json --out /tmp/plots    # one ablation's plots and FAQ entry
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import math
import os
import re
import statistics
import subprocess
import sys
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from guidance_plots import (
    ERROR_RATE_METRICS,
    LOWER_IS_BETTER,
    RATIO_METRICS,
    SHARE_METRICS,
    draw_by_size,
    draw_plot,
    draw_scatter,
    fmt,
    lower_name,
    money,
    times,
    x_axes_differ,
)

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "benchmarks" / "matrices" / "guidance_ablations.yaml"
RESULTS = Path(
    os.environ.get("GUIDANCE_RESULTS_DIR")
    or REPO / "benchmarks" / "results" / "guidance"
)
PRICING = REPO / "config_library" / "pricing.yaml"
COST = "cost_per_1000_docs"
MCOE_PAGE = "marginal-cost-of-error.md"
#: config_library/ sits outside docs/, so the docs site rewrites this to a blob URL.
PRICING_FROM_PAGES = "../../config_library/pricing.yaml"
COLD_NOTE = (
    "*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt "
    "cache the first run wrote, which a document processed once would not.*"
)
MCOE_CAPTION = (
    f" Lines give the [marginal cost of error]({MCOE_PAGE}) from the cheapest value: "
    "the extra cost per error prevented."
)
BANDS_CAPTION = "Bands show ±2 standard errors."
DIFFER_CAPTION = " Cost axes differ by dataset."
PAGES = REPO / "docs" / "benchmark-guidance"
#: Generated regions of a page. Each question's details are collapsed under its answer.
REGIONS = ("FAQ",)
PRESETS = "index.md#baseline-setup"
#: The heading the unmeasured questions of a page are listed under, at the FAQ's end.
PLANNED = "Planned"
DATASETS_PAGE = "datasets.md"
PLOTS = REPO / "images" / "benchmark-guidance"
PLOTS_FROM_PAGES = "../../images/benchmark-guidance"
EMPTY = "—"
CAPTION = (
    "Dots are the mean of the runs; shaded bands span two standard errors either side."
)
#: Pipeline steps named on every chart, in order.
MODEL_STEPS = ("OCR", "Classification", "Extraction", "Confidence")
VARIES = "varies (the setting under test)"
#: A step line the collector writes for a step that called nothing: a Bedrock step
#: with no calls billed, confidence off, or no OCR. A chart that names such a step's
#: model reads as though that model did the work, so it is left out.
NOT_RUN_SUFFIX = "(no calls billed)"
NOT_RUN = ("Off", "No OCR step")


def ran(text: str) -> bool:
    """Whether a step line describes a step that actually ran."""
    return not text.endswith(NOT_RUN_SUFFIX) and text not in NOT_RUN


#: The pipeline step each page is about, and how its lead line names that step's model.
PAGE_STEPS = {
    "digitization": ("OCR", "OCR"),
    "splitting-and-classification": ("Classification", "Classification model"),
    "extraction": ("Extraction", "Extraction model"),
    "token-use": ("Extraction", "Extraction model"),
    "confidence-assessment": ("Confidence", "Scoring model"),
}

sys.path.insert(0, str(REPO / "scripts"))
from check_markdown_links import slugify  # noqa: E402  (github-slugger rules)

Cell = str | dict[str, float]


def markers(region: str) -> tuple[str, str]:
    return (
        f"<!-- BEGIN GENERATED {region} -->",
        f"<!-- END GENERATED {region} -->",
    )


#: Reader-facing names that override the registry's: one name for the line-item score
#: everywhere a table, caption or chart names it.
METRIC_NAMES = {
    "list_score": {
        "name": "Line-item score",
        "short": "Line-item score",
        "axis": "Line items matched, line-item score (%)",
    },
}
#: The note under an answer measured on each dataset's preset, not the baseline.
INTERIM_NOTE = "*Measured on each dataset's preset, not the baseline setup.*"
#: A value as the registry names it, and as a reader sees it.
VALUE_NAMES = {"Default": "Preset default"}


def load_registry() -> dict[str, Any]:
    with REGISTRY.open(encoding="utf-8") as f:
        reg = interim_overlay(yaml.safe_load(f), RESULTS)
    for key, names in METRIC_NAMES.items():
        if key in reg.get("metrics", {}):
            reg["metrics"][key] = {**reg["metrics"][key], **names}
    return reg


def interim_overlay(reg: dict[str, Any], results_dir: Path) -> dict[str, Any]:
    # Interim results: a results file carrying `interim_ablation` (a full registry
    # entry) was measured against that entry, not the current one, so the entry it
    # carries replaces the registry's until a re-run overwrites the results file.
    ablations = reg.get("ablations") or []
    for i, ab in enumerate(ablations):
        path = results_dir / f"{ab['id']}.json"
        try:
            entry = json.loads(path.read_text(encoding="utf-8")).get("interim_ablation")
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict):
            # The question and a deprecation are the registry's own: a retitled
            # question keeps its new heading, and links to it, while its results
            # are interim. So are the cost a page shows and its economics, which are
            # how it reads the results rather than what was measured; but economics
            # the interim entry carries name its own values, so they stand.
            keep = {
                k: ab[k]
                for k in ("question", "deprecated", "cost_metric", "econ")
                if k in ab and not (k == "econ" and "econ" in entry)
            }
            ablations[i] = {**entry, "on_preset": True, **keep}
    return reg


def load_results(ablation_id: str) -> dict[str, Any] | None:
    path = RESULTS / f"{ablation_id}.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def shown_results(ab: dict[str, Any]) -> dict[str, Any] | None:
    """An ablation's results as its page shows them: none for a `deprecated` one,
    which is listed under Planned with its results kept but not shown."""
    return None if ab.get("deprecated") else load_results(ab["id"])


#: A dataset whose every value scores at least this weighted accuracy is too easy to
#: compare settings on: its chart is left out (ceiling_sets).
CEILING = 0.99
ALL_RIGHT = "all_fields_right"


def separates(found: list[Cell | None]) -> bool:
    """Whether the best and worst of these cells differ by more than clear_gap."""
    means = [c for c in found if isinstance(c, dict)]
    if len(means) < 2:
        return False
    hi = max(means, key=lambda c: c["mean"])
    lo = min(means, key=lambda c: c["mean"])
    return hi["mean"] - lo["mean"] > clear_gap(hi, lo)


def ceiling_sets(
    ab: dict[str, Any],
    cells: dict[str, Cell],
    kind_sets: list[str],
    plot_metrics: list[str],
) -> tuple[list[str], set[str]]:
    """The datasets too easy to chart, and those charted on every-field-right only.

    A dataset where every value scores CEILING or more weighted accuracy says nothing
    about the setting. When the chart also plots documents with every field right
    and that still separates the values, the dataset is charted on it alone.
    """
    ceiling: list[str] = []
    rows_only: set[str] = set()
    for k in kind_sets:
        found = [
            c
            for v in ab["values"]
            if isinstance(c := cells.get(f"{k}|{v}|weighted_accuracy"), dict)
        ]
        if not found or any(c["mean"] < CEILING for c in found):
            continue
        right = [cells.get(f"{k}|{v}|{ALL_RIGHT}") for v in ab["values"]]
        if ALL_RIGHT in plot_metrics and separates(right):
            rows_only.add(k)
        else:
            ceiling.append(k)
    return ceiling, rows_only


@cache
def load_pricing() -> dict[str, dict[str, float]]:
    """Unit prices from config_library/pricing.yaml, by `service/id` and unit."""
    with PRICING.open(encoding="utf-8") as f:
        entries = yaml.safe_load(f)["pricing"]
    return {
        e["name"]: {u["name"]: float(u["price"]) for u in e.get("units") or []}
        for e in entries
    }


@cache
def pricing_date() -> str:
    """The date config_library/pricing.yaml last changed, so --check stays stable.

    A date read from the commit is the same for everyone with the same tree. An
    uncommitted price list has no such date, so it falls back to today and says so.
    """
    rel = str(PRICING.relative_to(REPO))

    def git(*cmd: str) -> str:
        proc = subprocess.run(
            ["git", *cmd, "--", rel],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip()

    if git("status", "--porcelain"):
        print(
            f"warning: {rel} has uncommitted changes, so the price list has no commit "
            "date; the pages will say today's date",
            file=sys.stderr,
        )
        return datetime.date.today().isoformat()
    return git("log", "-1", "--format=%cs") or datetime.date.today().isoformat()


def where(region: str) -> str:
    return region or "the region we measured in"


def price_caveat(region: str) -> str:
    """The sentence every chart and table that shows a price carries."""
    return (
        f"Costs use on-demand prices for {where(region)} from "
        f"[config_library/pricing.yaml]({PRICING_FROM_PAGES}) as of {pricing_date()}."
    )


def price_caveat_svg(region: str) -> str:
    """The same caveat inside the image, so a chart shared on its own carries it."""
    return (
        f"Prices: on-demand, {where(region)}, config_library/pricing.yaml "
        f"as of {pricing_date()}"
    )


def cost_note(region: str, note: str = "") -> str:
    """The note under a table with a cost column: the price list, and what the cost
    counts (`note`, a cost metric's own, or the whole pipeline's by default)."""
    if note:
        return f"*{price_caveat(region)} {note}*"
    return (
        f"*{price_caveat(region)} Amazon Bedrock and Amazon Textract only; Lambda and "
        "other AWS charges are not included.*"
    )


def is_cost_metric(metric: str) -> bool:
    """Whether a metric is a cost per 1,000 documents: the whole pipeline's, or one
    step's (`<step>_cost_per_1000_docs`)."""
    return metric == COST or metric.endswith(f"_{COST}")


def cost_column(ab: dict[str, Any], kind: str, metrics: dict[str, Any]) -> str:
    """The cost an ablation shows for a dataset kind: its `cost_metric` where that
    applies to the kind, else the whole pipeline's."""
    chosen = ab.get("cost_metric")
    if not chosen:
        return COST
    if chosen not in metrics or not is_cost_metric(str(chosen)):
        raise SystemExit(f"{ab['id']}: cost_metric {chosen} is not a cost metric")
    return str(chosen) if kind in metrics[chosen]["kinds"] else COST


def cost_words(metric: str, metrics: dict[str, Any]) -> str:
    """What a cost metric counts, as a column names it: "Cost" for the whole pipeline,
    "Classification cost" for the classification step."""
    return "Cost" if metric == COST else metrics[metric]["name"].split(" per 1,000")[0]


def cost_axis_title(metric: str, metrics: dict[str, Any], unit: str) -> str:
    """A step's cost axis: "Classification cost per 1,000 packets ($)"; "" for the
    whole pipeline's, which the chart titles itself."""
    if metric == COST:
        return ""
    m = metrics[metric]
    return unit_name(m.get("axis") or m["name"].replace(" (USD)", " ($)"), unit)


def mean_se(values: list[float]) -> dict[str, float]:
    m = statistics.fmean(values)
    se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return {"mean": round(m, 6), "se": round(se, 6)}


def unit_price(name: str, unit: str) -> float | None:
    """The price of one unit, falling back from a `:1m` model ID to its base ID."""
    prices = load_pricing()
    for key in (name, name.removesuffix(":1m")):
        if unit in prices.get(key, {}):
            return prices[key][unit]
    return None


def long_context_surcharges(results: dict[str, Any] | None) -> dict[str, list[float]]:
    """Each `dataset|value`'s long-context surcharge per document, per run, in the
    order of its `usage` runs (the results file's `long_context`)."""
    return {
        key: [float(v) for v in entry.get("surcharge") or []]
        for key, entry in ((results or {}).get("long_context") or {}).items()
    }


def price_usage(
    usage: dict[str, list[dict[str, float]]],
    basis: dict[str, dict[str, Any]],
    surcharge: dict[str, list[float]] | None = None,
) -> tuple[dict[str, dict[str, float]], dict[str, set[str]], set[str]]:
    """Cost per 1,000 documents for each `dataset|value`, and its unpriced usage.

    Each run's usage is priced on its own. `basis` says which of them count: a value
    whose runs could share a prompt cache is priced from its coldest run alone, with
    no standard error, and is named in the returned set so the page can footnote it.
    Usage with no price counts as $0 and is returned so the page can say so.

    `surcharge` is what long-context pricing adds per document, per run in the order of
    the usage runs: pricing.yaml has one rate per unit, and Claude Haiku 5.5 bills a
    request over 100,000 prompt tokens at five times it. The collector sizes the
    requests from per-document metering (collect_guidance_results.py) and writes it as
    the results file's `long_context`, so the cost priced here matches the documents'
    own costs and the classification cost, which include it too.
    """
    costs: dict[str, dict[str, float]] = {}
    unpriced: dict[str, set[str]] = {}
    cold: set[str] = set()
    for key, runs in usage.items():
        per_run = []
        added = (surcharge or {}).get(key) or []
        if added and len(added) != len(runs):
            raise SystemExit(
                f"{key}: {len(added)} long-context surcharges for {len(runs)} runs"
            )
        for i, run in enumerate(runs):
            total = added[i] if added else 0.0
            for name, amount in run.items():
                service_id, _, unit = name.rpartition("|")
                price = unit_price(service_id, unit)
                if price is None:
                    if amount:
                        unpriced.setdefault(key, set()).add(name)
                    continue
                total += amount * price
            per_run.append(total * 1000)
        if not per_run:
            continue
        entry = basis.get(key) or {}
        index = int(entry.get("index") or 0)
        if entry.get("mode") == "cold-run" and 0 <= index < len(per_run):
            costs[key] = {"mean": round(per_run[index], 6), "se": 0.0}
            cold.add(key)
        else:
            costs[key] = mean_se(per_run)
    return costs, unpriced, cold


def model_steps(
    models: dict[str, dict[str, Any]], ds_keys: list[str], values: list[str]
) -> dict[str, dict[str, str]]:
    """The model at each step, per dataset, over a set of values.

    A step whose model or setting differs between those values is the one under test,
    so it reads "varies" rather than naming one of them. A step that ran for none of
    the values (ran()) is left out.
    """
    out: dict[str, dict[str, str]] = {}
    for ds_key in ds_keys:
        seen: dict[str, list[str]] = {}
        for value in values:
            entry = models.get(f"{ds_key}|{value}") or {}
            for step in MODEL_STEPS:
                text = entry.get(step)
                if text:
                    seen.setdefault(step, []).append(str(text))
        shown = {
            step: texts[0] if len(set(texts)) == 1 else VARIES
            for step, texts in seen.items()
            if any(ran(t) for t in texts)
        }
        if shown:
            out[ds_key] = shown
    return out


#: A step as a caption or table names it: the confidence step is the scoring model.
STEP_LABELS = {"Confidence": "Scoring"}


def models_text(steps: dict[str, str]) -> str:
    return " · ".join(
        f"{STEP_LABELS.get(s, s)}: {steps[s]}" for s in MODEL_STEPS if s in steps
    )


def models_sentence(
    steps: dict[str, dict[str, str]], ds_keys: list[str], datasets: dict[str, Any]
) -> str:
    """ "Models: ..." for a caption or a table row.

    Steps every dataset here ran the same way are stated once; a step two datasets
    ran differently, because their presets differ, is listed per dataset.
    """
    present = [k for k in ds_keys if k in steps]
    if not present:
        return ""
    common: list[str] = []
    parts: list[str] = []
    for step in MODEL_STEPS:
        texts = [steps[k][step] for k in present if step in steps[k]]
        if not texts:
            continue
        if len(texts) < len(present) or len(set(texts)) > 1:
            parts.append(
                f"{STEP_LABELS.get(step, step)} by dataset: "
                + "; ".join(
                    f"{datasets[k].get('short', datasets[k]['name'])} — {steps[k][step]}"
                    for k in present
                    if step in steps[k]
                )
            )
        else:
            common.append(f"{STEP_LABELS.get(step, step)}: {texts[0]}")
    if common:
        parts.insert(0, " · ".join(common))
    return "Models: " + ". ".join(parts) + "." if parts else ""


def span(values: list[int]) -> str:
    lo, hi = min(values), max(values)
    return f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"


def sample_sizes(
    sample: dict[str, dict[str, Any]], ds_keys: list[str], values: list[str]
) -> dict[str, tuple[str, str, int]]:
    """Documents scored, runs, and failed documents per run, per dataset."""
    out: dict[str, tuple[str, str, int]] = {}
    for ds_key in ds_keys:
        docs: list[int] = []
        failed: list[int] = []
        runs: list[int] = []
        for value in values:
            entry = sample.get(f"{ds_key}|{value}") or {}
            docs += [int(d) for d in entry.get("docs") or []]
            failed += [int(f) for f in entry.get("failed") or []]
            if entry.get("runs"):
                runs.append(int(entry["runs"]))
        if docs and runs:
            out[ds_key] = (span(docs), span(runs), max(failed or [0]))
    return out


def n_short(entry: tuple[str, str, int], unit: str = "documents") -> str:
    docs, runs, _ = entry
    return f"{docs} {unit} × {runs} runs"


def n_full(entry: tuple[str, str, int], unit: str = "documents") -> str:
    """The sample size, and what a failed document took out of it."""
    failed = entry[2]
    if not failed:
        return n_short(entry, unit)
    one = unit.removesuffix("s")
    if failed == 1:
        return f"{n_short(entry, unit)}; a {one} that failed in a run is left out of that run"
    return (
        f"{n_short(entry, unit)}; up to {failed} {unit} failed in a run and are left "
        "out of it"
    )


def sample_sentence(
    sizes: dict[str, tuple[str, str, int]],
    ds_keys: list[str],
    datasets: dict[str, Any],
) -> str:
    """ "N = ..." for a caption or a table row, per dataset when they differ."""
    present = [k for k in ds_keys if k in sizes]
    if not present:
        return ""
    texts = {k: n_full(sizes[k], per_unit(datasets[k]["kind"])) for k in present}
    if len(set(texts.values())) == 1:
        return f"N = {texts[present[0]]}."
    return (
        "N: "
        + "; ".join(
            f"{datasets[k].get('short', datasets[k]['name'])} — {texts[k]}"
            for k in present
        )
        + "."
    )


#: Model names as the collector writes them into a step's line.
MODEL_NAME = re.compile(
    r"Amazon Nova(?: \d+)? (?:Lite|Pro|Premier|Micro)"
    r"|Claude (?:Haiku|Sonnet|Opus) \d+(?:\.\d+)?"
    r"|OpenAI GPT-[\w.]+(?: [A-Z]\w+)?"
    r"|xAI Grok \d+(?:\.\d+)?"
)


def short_model(text: str) -> str:
    """A step's models without its settings: "Claude Sonnet 5, single-pass" to the model.

    One model reads as its name, wherever the line mentions it ("Amazon Nova 2 Lite
    finds the sections, then Amazon Nova 2 Lite classifies them" is Amazon Nova 2
    Lite). An escalation keeps its second model, because the step runs it too. A line
    naming no model ("Off") is returned as it is.
    """
    names = list(dict.fromkeys(MODEL_NAME.findall(text)))
    if not names:
        return text
    if len(names) == 2 and "escalating to" in text:
        return f"{names[0]}, escalating to {names[1]}"
    return " and ".join(names)


def lead_line(
    page: str, models: dict[str, dict[str, Any]], ds_key: str, values: list[str]
) -> str:
    """The model for the page's own step, which a chart names before anything else.

    A reader of a classification chart looks for the classification model; without
    this line the first model they find in the subtitle may be another step's. It
    names the model alone, so a setting of the step that varies (page by page or
    holistic) still leaves one model to name; it reads "varies" only when the model
    itself does.
    """
    step, label = PAGE_STEPS.get(page, ("", ""))
    texts = {
        str(text) if step == "OCR" else short_model(str(text))
        for value in values
        if (text := (models.get(f"{ds_key}|{value}") or {}).get(step))
        and ran(str(text))
    }
    if not texts:
        return ""
    if len(texts) > 1:
        return f"{label}: varies — see the key"
    return f"{label}: {texts.pop()}"


def panel_subtitle(
    steps: dict[str, str],
    size: tuple[str, str, int] | None,
    lead: str = "",
    unit: str = "documents",
) -> tuple[str, str]:
    """The lines under a panel title: the page's own model, then every step and N."""
    parts = [models_text(steps)] if steps else []
    if size:
        parts.append(f"N = {n_short(size, unit)}")
    return lead, " · ".join(p for p in parts if p)


#: How a chart's subtitle names a step: "Fixed: extraction on Claude Sonnet 5".
STEP_WORDS = {
    "OCR": "OCR",
    "Classification": "classification",
    "Extraction": "extraction",
    "Confidence": "scoring",
}
#: A model's vendor, which a point's label leaves out: the legend carries the family.
VENDOR = re.compile(
    r"\b(?:Amazon|Claude|OpenAI|xAI) (?=(?:Nova|Haiku|Sonnet|Opus|GPT|Grok)\b)"
)
DEFAULT_MARK = re.compile(r"\s*\((?:[^()]*,\s*)?default\)", re.IGNORECASE)


def default_value(ab: dict[str, Any]) -> str | None:
    """The ablation's default: the first value that names itself the default."""
    return next((v for v in ab["values"] if "default" in v.lower()), None)


def short_label(value: str, ab: dict[str, Any]) -> str:
    """The name a point is labelled with: "Holistic · Haiku 5.5 (default)".

    `short_values` in the registry maps a value, or one " · " part of it (a grid row),
    to a short form; a model name drops its vendor. The default value is marked.
    """
    shorts: dict[str, str] = ab.get("short_values") or {}
    if value in VALUE_NAMES:
        return VALUE_NAMES[value]
    if value in shorts:
        text = shorts[value]
    else:
        parts = [DEFAULT_MARK.sub("", p).strip() for p in value.split(" · ")]
        text = " · ".join(shorts.get(p, VENDOR.sub("", p)) for p in parts)
    text = DEFAULT_MARK.sub("", text).strip()
    if value == default_value(ab) and "default" not in text.lower():
        text += " (default)"
    return text


def plain(text: str) -> str:
    """A registry phrase without its code: "Schema enforcement (`x.y`)" to the words."""
    text = re.sub(r"\s*\([^()]*`[^()]*\)", "", text)
    return text.replace("`", "")


def varies_text(ab: dict[str, Any]) -> str:
    """What changes between the points, in plain words for a chart's subtitle."""
    return str(ab.get("varies") or lower_name(plain(ab["changes"])))


def fixed_model(
    ab: dict[str, Any], models: dict[str, dict[str, Any]], ds_key: str
) -> str:
    """The model held fixed that a reader of the page needs: "extraction on X".

    The page's own step when it ran the same way for every value; otherwise (its
    model or a setting of it is what varies) the extraction model, when that is the
    same for every value; otherwise nothing. The registry's `fixed` names the step
    instead, for a setting the step's line does not record.
    """
    values: list[str] = ab["values"]
    step = str(ab.get("fixed") or PAGE_STEPS.get(ab["page"], ("", ""))[0])

    def texts(s: str) -> set[str]:
        return {
            str(text)
            for value in values
            if (text := (models.get(f"{ds_key}|{value}") or {}).get(s))
            and ran(str(text))
        }

    own = texts(step) if step else set()
    if len(own) == 1 and step != "Extraction":
        return f"{STEP_WORDS[step]} on {own.pop()}"
    extraction = {short_model(t) for t in texts("Extraction")}
    if len(extraction) == 1:
        return f"extraction on {extraction.pop()}"
    return ""


def chart_name(key: str, datasets: dict[str, Any]) -> str:
    """A dataset as a chart names it: "RealKIE invoices"."""
    ds = datasets[key]
    return str(ds.get("chart") or ds.get("short") or ds["name"])


def per_unit(kind: str) -> str:
    """What a cost is counted per: a DocSplit packet is one document of pages."""
    return "packets" if kind.startswith("packet") else "documents"


def axis_title(key: str, metrics: dict[str, Any]) -> str:
    """A y axis in plain words, with its unit and the direction that is better."""
    m = metrics[key]
    text = m.get("axis") or (f"{m['name']} (%)" if key in SHARE_METRICS else m["name"])
    return f"{text} {'↓' if key in LOWER_IS_BETTER else '↑'}"


def n_chart(entry: tuple[str, str, int]) -> str:
    docs, runs, _ = entry
    return f"{docs} × {runs} runs"


def cell_text(cell: Cell | None, metric: str = "") -> str:
    if cell is None:
        return EMPTY
    if isinstance(cell, str):
        return cell
    if is_cost_metric(metric):
        if not cell.get("se"):
            return money(cell["mean"])
        return f"{money(cell['mean'])} ± {money(2 * cell['se'])}"
    if metric in RATIO_METRICS:
        return f"{times(cell['mean'])} ± {times(2 * cell.get('se', 0))}"
    return f"{fmt(cell['mean'])} ± {fmt(2 * cell.get('se', 0))}"


#: Columns where a lower figure is better; columns in NO_BEST have no better direction.
LOWER_BETTER_COLUMNS = LOWER_IS_BETTER | {COST, "median_seconds"}
NO_BEST = {"list_rows", "cached_share", "alert_rate", "errors_caught_30_random"}


def direction(metric: str) -> int:
    """+1 where higher is better, -1 where lower is, 0 where neither is."""
    if metric in NO_BEST:
        return 0
    if (
        metric in LOWER_BETTER_COLUMNS
        or metric.endswith("_tokens")
        or is_cost_metric(metric)
    ):
        return -1
    return 1


def clear_gap(a: dict[str, float], b: dict[str, float]) -> float:
    """Twice the combined standard error of two means: the gap a winner must beat."""
    return 2 * math.hypot(float(a.get("se") or 0), float(b.get("se") or 0))


def winner(cells: dict[Any, Cell | None], metric: str) -> Any:
    """The key whose mean beats the runner-up's by more than clear_gap, or None.

    A tie, a single measured cell, or a column with no better direction has none.
    """
    sign = direction(metric)
    found = [(k, c) for k, c in cells.items() if isinstance(c, dict)]
    if not sign or len(found) < 2:
        return None
    found.sort(key=lambda kc: -sign * kc[1]["mean"])
    (top, a), (_, b) = found[0], found[1]
    return top if sign * (a["mean"] - b["mean"]) > clear_gap(a, b) else None


def bold(text: str, on: bool) -> str:
    return f"**{text}**" if on else text


BOLD_FIGURE = (
    "A bold figure beats every other in its column, for that dataset, by more than "
    "twice the combined standard error."
)


#: Packet metrics that are a share of the packet's sections, and of its pages. A
#: DocSplit packet holds several of each, so one error of them is one section or one
#: page, not one packet.
SECTION_METRICS = frozenset({"section_accuracy", "label_accuracy", "boundary_accuracy"})
PAGE_METRICS = frozenset({"page_accuracy"})
#: List metrics that are a share of a list's true rows, so one error of them is a row.
ROW_METRICS = frozenset({"completeness"})
#: The results-file key that counts each of those per document.
PER_DOC_COUNTS = {
    "section": "sections_per_doc",
    "page": "pages_per_doc",
    "row": "rows_per_doc",
}
#: Metrics whose errors the economics line can count (1 - metric is a share of them).
ECON_METRICS = ERROR_RATE_METRICS | SECTION_METRICS


def error_unit(kind: str, metric: str) -> str:
    """What one error of `metric` is on a dataset of `kind`: a section or a page of a
    DocSplit packet, or a row of a list, for the accuracies that are shares of those;
    otherwise one document's (or packet's) worth of wrong field weight
    (marginal-cost-of-error.md)."""
    if kind == "list" and metric in ROW_METRICS:
        return "row"
    if kind.startswith("packet"):
        if metric in SECTION_METRICS:
            return "section"
        if metric in PAGE_METRICS:
            return "page"
    return per_unit(kind).removesuffix("s")


def units_per_doc(results: dict[str, Any], unit: str, ds_key: str) -> float | None:
    """How many `unit`s one document of the dataset holds: 1 for a whole document,
    else the results file's mean count (`sections_per_doc`); None when it has none."""
    key = PER_DOC_COUNTS.get(unit)
    if not key:
        return 1.0
    count = (results.get(key) or {}).get(ds_key)
    return float(count) if count else None


def price_text(price: float) -> str:
    """A price per error, rounded the way money() rounds, with "under $0.01"."""
    return "under $0.01" if price < 0.005 else money(price)


def mcoe_cells(
    points: list[tuple[str, dict[str, float], dict[str, float]]],
    units: float = 1.0,
) -> tuple[str, dict[str, str]]:
    """The cheapest value, and each value's "Cost per error prevented" cell against it.

    A price only where both the accuracy gain and the extra cost beat clear_gap;
    "same cost" where the gain does and the cost does not; "no measurable gain" where
    the gain does not; and "—" for the cheapest value and anything no dearer. A gain
    in a share of a packet's sections is worth `units` sections a packet
    (units_per_doc), so the price is per section.
    """
    ref, rx, ry = min(points, key=lambda p: (p[1]["mean"], p[2]["mean"]))
    out: dict[str, str] = {}
    for value, x, y in points:
        extra = x["mean"] - rx["mean"]
        gain = y["mean"] - ry["mean"]
        if value == ref or extra <= 0:
            out[value] = EMPTY
        elif gain <= clear_gap(y, ry):
            out[value] = "no measurable gain"
        elif extra <= clear_gap(x, rx):
            out[value] = "same cost"
        else:
            out[value] = price_text(extra / 1000 / (gain * units))
    return ref, out


#: What one error of a packet metric is to a reader who has to find and fix it.
FIX_WORDS = {
    "section_accuracy": "a misfiled or mis-split document",
    "label_accuracy": "a misfiled document",
    "boundary_accuracy": "a mis-split document",
    "page_accuracy": "a misfiled page",
    "completeness": "a missing row",
}
#: Names that keep their capital in mid-sentence: vendors, products, acronyms.
PROPER = re.compile(r"(?:Amazon|Claude|OpenAI|xAI|Textract|Strands|[A-Z]{2})")
NO_DIFFERENCE = "no measurable difference"


def mid_sentence(text: str) -> str:
    """A value's name inside a sentence: "holistic", but "Claude Haiku 5.5"."""
    return text if PROPER.match(text) else lower_first(text)


def econ_names(ab: dict[str, Any], a: str, b: str) -> tuple[str, str]:
    """How the economics line names two values: the registry's `econ.names`, else
    each value without its note in parentheses and without the parts the two share
    ("Holistic · Claude Haiku 5.5" against "Holistic · Claude Sonnet 5.5" reads
    "Claude Haiku 5.5" and "Claude Sonnet 5.5")."""
    given: dict[str, str] = (ab.get("econ") or {}).get("names") or {}

    def parts(value: str) -> list[str]:
        name = re.sub(r"\s*\([^()]*\)$", "", VALUE_NAMES.get(value, value))
        return name.split(" · ")

    pa, pb = parts(a), parts(b)
    shared = set(pa) & set(pb)
    if len(shared) < min(len(pa), len(pb)):
        pa = [p for p in pa if p not in shared]
        pb = [p for p in pb if p not in shared]
    return given.get(a) or ", ".join(pa), given.get(b) or ", ".join(pb)


def econ_delta(
    cells: dict[str, Cell], ds_key: str, a: str, b: str, metric: str, cost: str
) -> dict[str, float] | None:
    """Value `b` against value `a`: the extra cost per 1,000 documents and the gain
    in `metric`, and the sign of each where it beats clear_gap (0 where it does not).
    None unless all four cells have a mean."""
    found = [cells.get(f"{ds_key}|{v}|{m}") for m in (cost, metric) for v in (a, b)]
    if not all(isinstance(c, dict) for c in found):
        return None
    ca, cb, ya, yb = (c for c in found if isinstance(c, dict))
    extra = cb["mean"] - ca["mean"]
    gain = yb["mean"] - ya["mean"]
    return {
        "extra": extra,
        "gain": gain,
        "cost_sign": math.copysign(1, extra) if abs(extra) > clear_gap(cb, ca) else 0,
        "gain_sign": math.copysign(1, gain) if abs(gain) > clear_gap(yb, ya) else 0,
    }


class Econ:
    """What the economics of an ablation count, for one dataset.

    `unit` is one error (a section, a document), `units` how many a document holds,
    `docs` what a cost is counted per, `metric` the accuracy, and `paid_for` what
    the cost covers (" of classification", or "" for the whole pipeline).
    """

    def __init__(
        self, unit: str, units: float, docs: str, metric: str, paid_for: str
    ) -> None:
        self.unit, self.units, self.docs = unit, units, docs
        self.metric, self.paid_for = metric, paid_for
        self.whole = unit in ("document", "packet")

    def count(self, delta: dict[str, float]) -> int:
        """Errors avoided (or added) per 1,000 documents."""
        return round(abs(delta["gain"]) * self.units * 1000)

    def price(self, delta: dict[str, float]) -> str:
        """The cost difference per error avoided (or added)."""
        return price_text(
            abs(delta["extra"]) / (abs(delta["gain"]) * self.units * 1000)
        )

    def errors(self, n: int) -> str:
        return f"{n:,} {self.unit}{'' if n == 1 else 's'}"


def econ_sentence(
    to: str, frm: str, delta: dict[str, float], e: Econ, lead: str = ""
) -> str:
    """The economics of `to` against `frm`: one sentence, and a second naming the
    cost of an error at which `to` pays for itself, when there is a price. `lead`
    opens the sentence ("On DocSplit packets, ")."""
    n = e.count(delta)
    noun = f"{e.unit}{'' if n == 1 else 's'}"
    dollars, price = money(abs(delta["extra"])), e.price(delta) if n else ""
    fix = (
        f"a wrong {e.unit}" if e.whole else f"finding and fixing {FIX_WORDS[e.metric]}"
    )
    a = lead + mid_sentence(to) if lead else to[:1].upper() + to[1:]
    b = mid_sentence(frm)
    swap, per = f"{a} instead of {b}", f"per 1,000 {e.docs}"
    spend = f"{dollars}{e.paid_for} {per}"
    cost, gain = delta["cost_sign"], delta["gain_sign"]
    if gain > 0 and cost > 0:
        each = f"per {e.unit} it gets right" if e.whole else f"per {e.unit} it saves"
        return (
            f"{swap} adds {spend} and gets {n:,} more {noun} right, so it costs "
            f"{price} {each}. Choose it when {fix} costs you more than {price}."
        )
    if gain < 0 and cost < 0:
        return (
            f"{swap} saves {spend} but gets {n:,} fewer {noun} right, so it saves "
            f"{price} per extra wrong {e.unit}. Choose it when {fix} costs you less "
            f"than {price}."
        )
    if gain > 0 and cost < 0:
        return f"{swap} saves {spend} and gets {n:,} more {noun} right."
    if gain > 0:
        return (
            f"{swap} gets {n:,} more {noun} right {per}, at no measurable extra cost."
        )
    if gain < 0 and cost > 0:
        return f"{swap} adds {spend} and gets {n:,} fewer {noun} right."
    if gain < 0:
        return f"{swap} gets {n:,} fewer {noun} right {per}, at no measurable saving."
    if cost > 0:
        return (
            f"{a} scored no measurably better than {b}, so the extra {spend} buys "
            "nothing measurable."
        )
    if cost < 0:
        return f"{swap} saves {spend} at no measurable loss."
    return f"{a} and {b} show {NO_DIFFERENCE} in cost or accuracy."


def econ_row(delta: dict[str, float], e: Econ) -> list[str]:
    """A row of the economics table: the extra cost and the extra errors avoided per
    1,000 documents, and what each costs (or the saving per extra error)."""
    extra, cost, gain = delta["extra"], delta["cost_sign"], delta["gain_sign"]
    n = e.count(delta)
    price = e.price(delta) if n else ""
    if gain > 0 and cost > 0:
        price_cell = price
    elif gain < 0 and cost < 0:
        price_cell = f"saves {price} per extra wrong {e.unit}"
    elif gain > 0:
        price_cell = "more right, for less" if cost < 0 else "more right, same cost"
    elif not gain and cost > 0:
        price_cell = "no measurable gain"
    elif not gain and cost < 0:
        price_cell = "cheaper, no measurable loss"
    else:
        price_cell = EMPTY
    return [
        f"{'+' if extra > 0 else '−'}{money(abs(extra))}" if cost else NO_DIFFERENCE,
        f"{'+' if gain > 0 else '−'}{n:,}" if gain else NO_DIFFERENCE,
        price_cell,
    ]


def render_econ(
    ab: dict[str, Any],
    reg: dict[str, Any],
    results: dict[str, Any],
    cells: dict[str, Cell],
) -> list[str]:
    """The economics under an answer: a sentence on the registry's `econ.line` value
    (or values) against `econ.against`, then a table of every other measured value
    against `against`.

    Each compares the cost a page shows (the `cost_metric`, else the whole pipeline's)
    with the plotted accuracy, counted as errors per 1,000 documents: (1 − accuracy)
    times the errors a document can hold, one for a document's fields, and the
    results file's `sections_per_doc` for a packet's section accuracy. A cost or gain
    within clear_gap is no measurable difference. The dataset is `econ.dataset`, else
    the first one that measured `against`. Empty, with a warning, when the results
    lack what it needs.
    """
    spec = ab.get("econ") or {}
    if not spec:
        return []
    datasets: dict[str, Any] = reg["datasets"]
    metrics: dict[str, Any] = reg["metrics"]
    against = str(spec["against"])
    lines = spec.get("line") or []
    lines = [lines] if isinstance(lines, str) else list(lines)

    def warn(text: str) -> list[str]:
        print(f"warning: {ab['id']}: econ: {text}", file=sys.stderr)
        return []

    if against not in ab["values"]:
        return warn(f"{against!r} is not one of its values")
    wanted = ab.get("plot") or []
    wanted = [wanted] if isinstance(wanted, str) else list(wanted)
    found = None
    for ds_key in [spec["dataset"]] if spec.get("dataset") else ab["datasets"]:
        kind = datasets[ds_key]["kind"]
        cols = [m for m in ab["metrics"] if kind in metrics[m]["kinds"]]
        metric = next((m for m in wanted if m in cols), cols[0] if cols else "")
        cost = cost_column(ab, kind, metrics)
        if all(
            isinstance(cells.get(f"{ds_key}|{against}|{m}"), dict)
            for m in (metric, cost)
        ):
            found = (ds_key, kind, metric, cost)
            break
    if found is None:
        return warn(f"no measured {against!r}")
    ds_key, kind, metric, cost = found
    if metric not in ECON_METRICS:
        return warn(f"{metric} is not an accuracy it can count errors in")
    unit = error_unit(kind, metric)
    units = units_per_doc(results, unit, ds_key)
    if units is None:
        return warn(f"no {PER_DOC_COUNTS[unit]} for {ds_key}; re-collect it")
    docs = per_unit(kind)
    words = cost_words(cost, metrics)
    paid_for = "" if cost == COST else f" of {lower_first(words.removesuffix(' cost'))}"
    e = Econ(unit, units, docs, metric, paid_for)
    others = {
        v: d
        for v in ab["values"]
        if v != against
        and (d := econ_delta(cells, ds_key, against, v, metric, cost)) is not None
    }
    out: list[str] = []
    said = []
    where = "" if len(ab["datasets"]) == 1 else f"On {chart_name(ds_key, datasets)}, "
    for value in lines:
        if value not in others:
            warn(f"no measured {value!r} to compare")
            continue
        frm, to = econ_names(ab, against, value)
        # Only the first sentence names the dataset.
        said.append(econ_sentence(to, frm, others[value], e, "" if said else where))
    if said:
        out += ["**The economics:** " + " ".join(said), ""]
    if not others:
        return out
    each = "it gets right" if e.whole else "it saves"
    out += [
        f"| Instead of {short_label(against, ab).replace(' (baseline setup)', '').replace(' (default)', '')} | "
        f"Extra {lower_first(words)} per 1,000 {docs} | "
        f"Extra {unit}s right per 1,000 {docs} | "
        f"[Cost per {unit} {each}]({MCOE_PAGE}) |",
        "|---|---|---|---|",
    ]
    for value, delta in others.items():
        out.append(
            f"| {short_label(value, ab)} | " + " | ".join(econ_row(delta, e)) + " |"
        )
    sizes = sample_sizes(results.get("sample") or {}, [ds_key], ab["values"])
    n = f", N = {n_short(sizes[ds_key], docs)}" if ds_key in sizes else ""
    out += [
        "",
        f"*{chart_name(ds_key, datasets)}{n}. A difference within twice the combined "
        "standard error is no measurable difference. "
        f"{price_caveat(str(results.get('region') or ''))}*",
        "",
    ]
    return out


def failures_line(
    sample: dict[str, dict[str, Any]],
    ds_keys: list[str],
    values: list[str],
    ab: dict[str, Any],
    datasets: dict[str, Any],
) -> str:
    """ "Failed packets per run: Page by page · Claude Haiku 5.5: 2, 2, 0; ..." for
    the values a run of which left out a failed document; empty when none did."""
    parts = []
    for ds_key in ds_keys:
        for value in values:
            failed = [
                int(f)
                for f in (sample.get(f"{ds_key}|{value}") or {}).get("failed") or []
            ]
            if not any(failed):
                continue
            name = VALUE_NAMES.get(value, value)
            if len(ds_keys) > 1:
                short = datasets[ds_key].get("short", datasets[ds_key]["name"])
                name = f"{short} — {name}"
            parts.append(f"{name}: {', '.join(str(f) for f in failed)}")
    if not parts:
        return ""
    unit = per_unit(datasets[ds_keys[0]]["kind"])
    return f"Failed {unit} per run: " + "; ".join(parts) + "."


def not_sent_line(
    not_sent: dict[str, dict[str, Any]],
    ds_keys: list[str],
    values: list[str],
    datasets: dict[str, Any],
) -> str:
    """ "Not sent: ..." for the values Bedrock refused, on every document, as too large
    for one request (the collector's `not_sent`); empty when there are none."""
    parts = []
    for ds_key in ds_keys:
        for value in values:
            found = not_sent.get(f"{ds_key}|{value}")
            if not found:
                continue
            short = datasets[ds_key].get("short", datasets[ds_key]["name"])
            parts.append(
                f"{short} — {VALUE_NAMES.get(value, value)}: Amazon Bedrock refused "
                f"every document's request in all {found['runs']} runs "
                f"({found['documents']} in all) as too large for one request "
                f"(`{found['error']}`), so nothing was extracted"
            )
    return f"Not sent: {'; '.join(parts)}." if parts else ""


def unit_name(name: str, unit: str) -> str:
    """A metric's name counted in the dataset's unit: per 1,000 packets for DocSplit."""
    if unit == "documents":
        return name
    one = unit.removesuffix("s")
    return name.replace("1,000 documents", f"1,000 {unit}").replace(
        "per document", f"per {one}"
    )


def lower_first(text: str) -> str:
    """Lower-case the first letter only, so "Cost ... (USD)" keeps its acronym."""
    return text[:1].lower() + text[1:]


def with_cost_column(cols: list[str], metric: str = COST) -> list[str]:
    """Add the cost column (`metric`) after the last tokens column, or at the end."""
    if metric in cols:
        return cols
    last = max((i for i, m in enumerate(cols) if m.endswith("_tokens")), default=-1)
    at = last + 1 if last >= 0 else len(cols)
    return [*cols[:at], metric, *cols[at:]]


def dataset_anchor(key: str, datasets: dict[str, Any]) -> str:
    return f"{DATASETS_PAGE}#{slugify(datasets[key]['name'])}"


def dataset_label(key: str, datasets: dict[str, Any], short: bool = False) -> str:
    ds = datasets[key]
    text = ds.get("short", ds["name"]) if short else ds["name"]
    return f"[{text}]({dataset_anchor(key, datasets)})"


LIST_SCORE_CAPTION = (
    "A line-item score is Stickler's score for the list: matched rows over the larger of "
    "the true and returned row counts."
)
#: What a by-size chart's metric is, under its chart.
BY_SIZE_CAPTIONS = {
    "list_score": LIST_SCORE_CAPTION,
    "completeness": (
        "Completeness is the share of the ground truth's rows that came back, each "
        "returned row joined to its true row by the SEQ tag in its description."
    ),
}


def dataset_pools(ds_keys: list[str]) -> list[list[str]]:
    """Dataset keys grouped into pools that draw one chart between them.

    Keys whose first word is the same are the same pool of documents: realkie,
    realkie_short and realkie_long are three test sets over one set of invoices, and
    three charts of 20 documents each say less than one chart of all of them.
    """
    pools: dict[str, list[str]] = {}
    for key in ds_keys:
        pools.setdefault(key.split("_", 1)[0], []).append(key)
    return list(pools.values())


def by_size_owners(
    pool: list[str],
    values: list[str],
    per_doc: dict[str, dict[str, dict[str, float]]],
    sample: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """The test set each document of the pool is counted under.

    A document in more than one of the pool's test sets belongs to the one that ran
    it most often, so the same invoice is one point per value rather than three.
    """

    def runs(ds_key: str) -> int:
        return sum(
            int((sample.get(f"{ds_key}|{value}") or {}).get("runs") or 0)
            for value in values
        )

    owner: dict[str, str] = {}
    for ds_key in sorted(pool, key=lambda k: (-runs(k), pool.index(k))):
        for value in values:
            for doc in per_doc.get(f"{ds_key}|{value}") or {}:
                owner.setdefault(doc, ds_key)
    return owner


def by_size_series(
    owner: dict[str, str],
    values: list[str],
    per_doc: dict[str, dict[str, dict[str, float]]],
    metric: str,
    x_metric: str,
) -> list[tuple[str, list[tuple[str, float, float]]]]:
    """One series per value: every document that has both scores, smallest first."""
    out = []
    for value in values:
        pts = []
        for doc, ds_key in owner.items():
            entry = (per_doc.get(f"{ds_key}|{value}") or {}).get(doc) or {}
            x, y = entry.get(x_metric), entry.get(metric)
            # A log axis has no room for a document whose list is empty.
            if x is not None and y is not None and x >= 1:
                pts.append((doc, float(x), float(y)))
        if pts:
            out.append((value, sorted(pts, key=lambda p: p[1])))
    return out


def pool_sources(
    pool: list[str], values: list[str], per_doc: dict[str, Any]
) -> list[str]:
    """The datasets of a pool to draw from.

    A pool's `<prefix>_all` dataset holds every document the subsets do, so once it
    has per-document results it stands for the pool alone; until then the subsets
    merge (by_size_owners).
    """
    whole = [
        k
        for k in pool
        if k.endswith("_all") and any(per_doc.get(f"{k}|{v}") for v in values)
    ]
    return whole[:1] or [k for k in pool if not k.endswith("_all")]


#: Extraction modes as the collector's Extraction line names them, in column order.
MODES = ("single-pass", "agentic")


def value_modes(
    models: dict[str, dict[str, Any]], ds_keys: list[str], values: list[str]
) -> dict[str, tuple[str, str]]:
    """Each value's extraction model and mode, read from its Extraction line.

    The line reads "<model>, <mode>[, ...]" (collect_guidance_results.extraction_line).
    A value whose line names no mode, or whose runs used more than one configuration,
    is left out.
    """
    out: dict[str, tuple[str, str]] = {}
    for value in values:
        for ds_key in ds_keys:
            text = str((models.get(f"{ds_key}|{value}") or {}).get("Extraction") or "")
            parts = text.split(", ")
            if " ; " not in text and len(parts) > 1 and parts[1] in MODES:
                out[value] = (short_model(parts[0]), parts[1])
                break
    return out


def mode_label(value: str, modes: dict[str, tuple[str, str]]) -> str:
    """ "Agentic · Claude Sonnet 5": the mode and model, which every value has."""
    if value not in modes:
        return value
    model, mode = modes[value]
    return f"{mode[:1].upper()}{mode[1:]} · {model}"


def series_slots(
    names: list[str], modes: dict[str, tuple[str, str]]
) -> list[tuple[int, int]] | None:
    """Color by mode and shape by model, when the values are modes of several models.

    Six values in six cycled colors would be read by color alone; two colors for the
    two modes and a shape (and a line style) per model keep each one legible.
    """
    if not names or any(n not in modes for n in names):
        return None
    order = list(dict.fromkeys(modes[n][0] for n in names))
    if len(order) < 2 or len(order) > 3:
        return None
    return [(MODES.index(modes[n][1]), order.index(modes[n][0])) for n in names]


def size_bands(
    bands: Any, series: list[tuple[str, list[tuple[str, float, float]]]]
) -> list[int] | None:
    """A by-size chart's bands: `each` gives every size drawn a band of its own (a
    test set per list length), a list is lower bounds, and None leaves the default."""
    if bands != "each":
        return bands
    sizes = sorted({int(x) for _, pts in series for _, x, _ in pts})
    return [b for x in sizes for b in (x, x + 1)]


def render_by_size(
    ab: dict[str, Any], reg: dict[str, Any], results: dict[str, Any]
) -> tuple[list[list[str]], list[str], dict[str, str]]:
    """The by-size charts of an ablation that has them: each chart's markdown, the
    tables that go with them, and the plots.

    One chart per pool of datasets (dataset_pools), drawn from the results file's
    per-document scores: where a mean over a dataset says which value wins, this says
    at what size it starts winning. An ablation with `strata` gets its table under
    each chart (render_strata).
    """
    spec = ab.get("by_size") or {}
    per_doc = results.get("per_doc") or {}
    if not spec or not per_doc:
        return [], [], {}
    datasets: dict[str, Any] = reg["datasets"]
    metrics: dict[str, Any] = reg["metrics"]
    metric, x_metric = str(spec["metric"]), str(spec["x"])
    models = results.get("models") or {}
    sample = results.get("sample") or {}
    charts: list[list[str]] = []
    tables: list[str] = []
    plots: dict[str, str] = {}
    for pool in dataset_pools(ab["datasets"]):
        sources = pool_sources(pool, ab["values"], per_doc)
        owner = by_size_owners(sources, ab["values"], per_doc, sample)
        series = by_size_series(owner, ab["values"], per_doc, metric, x_metric)
        if not series:
            continue
        used = sorted(set(owner.values()), key=sources.index)
        drawn = sorted({doc for _, pts in series for doc, _, _ in pts})
        runs = span(
            [
                int((sample.get(f"{k}|{v}") or {}).get("runs") or 0)
                for k in used
                for v in ab["values"]
                if (sample.get(f"{k}|{v}") or {}).get("runs")
            ]
            or [ab.get("runs", 0)]
        )
        y_name = metrics[metric]["name"]
        strata = ab.get("strata") or {}
        things = str(strata.get("documents") or "documents")
        n_text = f"N = {len(drawn)} {things} × {runs} runs"
        thing = things.removesuffix("s")
        x_title = (
            f"{str(strata['noun'])[:1].upper()}{str(strata['noun'])[1:]} per {thing} "
            "(log scale)"
            if strata.get("noun")
            else f"{metrics[x_metric].get('axis') or metrics[x_metric]['name']} "
            "(log scale)"
        )
        modes = value_modes(models, used, ab["values"])
        slots = series_slots([v for v, _ in series], modes)
        # `points: runs` draws each document's score in each run (the results'
        # `<metric>_runs`), and a value never sent at a size says so there.
        by_runs = spec.get("points") == "runs"
        run_scores = (
            [
                {
                    doc: list(
                        ((per_doc.get(f"{owner[doc]}|{v}") or {}).get(doc) or {}).get(
                            f"{metric}_runs"
                        )
                        or [y]
                    )
                    for doc, _, y in pts
                }
                for v, pts in series
            ]
            if by_runs
            else None
        )
        refused = [
            (s, float(size))
            for key in results.get("not_sent") or {}
            for s, (v, _) in enumerate(series)
            if key.split("|", 1) == [key.split("|", 1)[0], v]
            and key.split("|", 1)[0] in sources
            and (size := (results.get("rows_per_doc") or {}).get(key.split("|")[0]))
        ]
        series = [(short_label(v, ab), pts) for v, pts in series]
        fixed = fixed_model(ab, models, used[0])
        subtitle = (
            f"{spec.get('name') or chart_name(pool[0], datasets)}, "
            f"{len(drawn)} × {runs} runs · "
            f"Varies: {varies_text(ab)}" + (f" · Fixed: {fixed}" if fixed else "")
        )
        title = str(ab.get("takeaway") or ab["question"])
        name = f"{ab['id']}-by-size-{pool[0].replace('_', '-')}.svg"
        plots[name] = draw_by_size(
            x_title,
            axis_title(metric, metrics),
            series,
            title=title,
            subtitle=subtitle,
            slots=slots,
            y_name=y_name,
            bands=size_bands(spec.get("bands"), series),
            runs=run_scores,
            not_sent=refused,
        )
        alt = (
            f"{title}: {lower_name(y_name)} per {thing} against "
            f"{lower_first(x_title.removesuffix(' (log scale)'))}"
        )
        caption = (
            f"*One small point per {thing} per run, for each {varies_text(ab)}; the "
            "large mark is the mean of those runs at each size, matching the table, and "
            "each line joins the means.*"
            if by_runs
            else f"*One point per {thing} per {varies_text(ab)}; each line is its "
            "running median.*"
        )
        charts.append([f"![{alt}]({PLOTS_FROM_PAGES}/{name})", "", caption, ""])
        tables += [
            f"*{n_text}. Datasets: "
            + ", ".join(dataset_label(k, datasets, short=True) for k in used)
            + "."
            + (f" {BY_SIZE_CAPTIONS[metric]}" if metric in BY_SIZE_CAPTIONS else "")
            + "*",
            "",
        ]
        if ab.get("strata"):
            tables += render_strata(ab, reg, results, sources)
    return charts, tables, plots


def band_labels(bands: list[int], noun: str) -> list[str]:
    """ "No line items", "1–20", ..., "More than 300", for bands given as lower bounds."""
    out = []
    for i, lo in enumerate(bands):
        hi = bands[i + 1] - 1 if i + 1 < len(bands) else None
        if hi is None:
            out.append(f"More than {lo - 1:,}" if lo > 0 else "Any")
        elif lo == hi == 0:
            out.append(f"No {noun}")
        elif lo == hi:
            out.append(f"{lo:,}")
        else:
            out.append(f"{lo:,}–{hi:,}")
    return out


def band_of(x: float, bands: list[int]) -> int:
    return max(i for i, lo in enumerate(bands) if x >= lo) if x >= bands[0] else -1


def strata_docs(
    sources: list[str],
    values: list[str],
    per_doc: dict[str, dict[str, dict[str, float]]],
    sample: dict[str, dict[str, Any]],
    score: str,
    by: str,
) -> tuple[list[str], dict[str, tuple[str, float]]]:
    """The values with per-document results, and the documents every one of them scored.

    Each document is counted once, under the first source dataset (most runs first)
    in which every value has its `score`; `by` comes from the same entries. Returns
    the values and {document: (dataset, by)}.
    """
    shown = [v for v in values if any(per_doc.get(f"{k}|{v}") for k in sources)]

    def runs(ds_key: str) -> int:
        return sum(
            int((sample.get(f"{ds_key}|{v}") or {}).get("runs") or 0) for v in shown
        )

    docs: dict[str, tuple[str, float]] = {}
    for ds_key in sorted(sources, key=lambda k: (-runs(k), sources.index(k))):
        entries = [per_doc.get(f"{ds_key}|{v}") or {} for v in shown]
        for doc in entries[0] if entries else {}:
            if doc in docs:
                continue
            found = [e.get(doc) or {} for e in entries]
            size = next((f[by] for f in found if by in f), None)
            if size is not None and all(score in f for f in found):
                docs[doc] = (ds_key, float(size))
    return shown, docs


def ratio_text(num: float | None, den: float | None) -> str:
    if num is None or not den:
        return EMPTY
    return f"{num / den:.1f}×"


def render_strata(
    ab: dict[str, Any], reg: dict[str, Any], results: dict[str, Any], sources: list[str]
) -> list[str]:
    """A pool's documents banded by size: one table per set of models that scored
    the same documents (strata_table).

    A table counts only documents every value in it scored, so a model run on fewer
    of the pool's documents than another gets a table of its own instead of shrinking
    the other's to its documents.
    """
    spec = ab["strata"]
    per_doc = results.get("per_doc") or {}
    sample = results.get("sample") or {}
    models = results.get("models") or {}
    by, score = str(spec["by"]), str(spec.get("score") or ab["by_size"]["metric"])
    values = [v for v in ab["values"] if any(per_doc.get(f"{k}|{v}") for k in sources)]
    modes = value_modes(models, sources, values)
    # Values grouped by model, in the registry's order; a value with no mode is its
    # own group.
    groups: dict[str, list[str]] = {}
    for v in values:
        groups.setdefault(modes[v][0] if v in modes else v, []).append(v)
    tables: list[tuple[frozenset[str], list[str]]] = []
    for group in groups.values():
        _, docs = strata_docs(sources, group, per_doc, sample, score, by)
        if not docs:
            continue
        found = frozenset(docs)
        same = next((t for t in tables if t[0] == found), None)
        if same:
            same[1].extend(group)
        else:
            tables.append((found, list(group)))
    out: list[str] = []
    for _, group in tables:
        out += strata_table(ab, reg, results, sources, group)
    if out:
        out += [cost_note(str(results.get("region") or "")), ""]
    return out


StrataMeans = Callable[[list[str], str, str], float | None]


def per_doc_cell(scores: list[float]) -> dict[str, float] | None:
    """A mean and standard error over per-document scores; None under two scores."""
    if len(scores) < 2:
        return None
    return {
        "mean": statistics.fmean(scores),
        "se": statistics.stdev(scores) / math.sqrt(len(scores)),
    }


def strata_markdown(
    values: list[str],
    labels: dict[str, str],
    pairs: list[tuple[str, str, str]],
    rows: list[tuple[str, list[str]]],
    means: StrataMeans,
    found: Callable[[list[str], str, str], list[float]],
    score: str,
    score_name: str,
    noun: str,
    things: str,
) -> list[str]:
    """The strata as one Markdown table, every value side by side (values that do
    not pair into single-pass and agentic for one model)."""
    header = [noun[:1].upper() + noun[1:], things[:1].upper() + things[1:]]
    for v in values:
        header += [
            f"{labels[v]}: {score_name}",
            f"{labels[v]}: weighted accuracy",
            f"{labels[v]}: cost per 1,000",
        ]
    header += [
        "Agentic costs" if len(pairs) == 1 else f"Agentic costs, {model}"
        for model, _, _ in pairs
    ]
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for label, group in rows:
        if not group:
            continue
        scores = {v: means(group, v, score) for v in values}
        best = winner({v: per_doc_cell(found(group, v, score)) for v in values}, score)
        row = [
            label if not label.startswith("All ") else f"**{label}**",
            f"{len(group):,}",
        ]
        for v in values:
            m = scores[v]
            text = bold(EMPTY if m is None else f"{m:.2f}", best == v)
            acc = means(group, v, "weighted_accuracy")
            cost = means(group, v, "cost")
            row += [
                text,
                EMPTY if acc is None else f"{acc:.2f}",
                EMPTY if cost is None else money(cost * 1000),
            ]
        for _, single, agent in pairs:
            row.append(
                ratio_text(means(group, agent, "cost"), means(group, single, "cost"))
            )
        out.append("| " + " | ".join(row) + " |")
    return out


def strata_html(
    model: str,
    single: str,
    agent: str,
    rows: list[tuple[str, list[str]]],
    found: Callable[[list[str], str, str], list[float]],
    score: str,
    score_name: str,
    noun: str,
    things: str,
    sizes: dict[str, float],
) -> list[str]:
    """One model's strata as an HTML table: a header row of metric groups, each
    with single-pass and agentic side by side, and a rule before each group
    (`table.strata` in docs-site/src/styles/custom.css).

    The better score of a row is bold only where the gap exceeds twice its standard
    error, from the per-document scores of the row's documents. A row of documents
    with no list (every size 0) has no score.
    """

    def means(group: list[str], value: str, metric: str) -> float | None:
        scores = found(group, value, metric)
        return statistics.fmean(scores) if scores else None

    def se(scores: list[float]) -> float | None:
        if len(scores) < 2:
            return None
        return statistics.stdev(scores) / math.sqrt(len(scores))

    pair = (single, agent)
    groups = [
        score_name[:1].upper() + score_name[1:],
        "Weighted accuracy",
        f"Cost per 1,000 {things}",
    ]
    out = [
        '<table class="strata">',
        f"<caption>{html.escape(model)}</caption>",
        "<thead>",
        "<tr>"
        f'<th rowspan="2">{html.escape(noun[:1].upper() + noun[1:])}</th>'
        f'<th rowspan="2">{html.escape(things[:1].upper() + things[1:])}</th>'
        + "".join(
            f'<th colspan="2" class="group">{html.escape(g)}</th>' for g in groups
        )
        + '<th rowspan="2" class="group">Agentic costs</th>'
        "</tr>",
        "<tr>"
        + '<th class="group">Single\u2011pass</th><th>Agentic</th>' * len(groups)
        + "</tr>",
        "</thead>",
        "<tbody>",
    ]

    def cells(texts: list[str]) -> str:
        return f'<td class="group">{texts[0]}</td><td>{texts[1]}</td>'

    for label, group in rows:
        if not group:
            continue
        per = [found(group, v, score) for v in pair]
        if all(sizes.get(d) == 0 for d in group):
            per = [[], []]
        scores = [statistics.fmean(s) if s else None for s in per]
        errors = [se(s) for s in per]
        better = None
        if None not in scores and None not in errors:
            a, b = (float(x) for x in scores if x is not None)
            ea, eb = (float(x) for x in errors if x is not None)
            if abs(a - b) > 2 * math.sqrt(ea**2 + eb**2):
                better = 0 if a > b else 1
        score_texts = []
        for i, m in enumerate(scores):
            text = EMPTY if m is None else f"{m:.2f}"
            score_texts.append(f"<strong>{text}</strong>" if i == better else text)
        acc = [means(group, v, "weighted_accuracy") for v in pair]
        cost = [means(group, v, "cost") for v in pair]
        name = html.escape(label)
        out.append(
            "<tr>"
            f"<td>{f'<strong>{name}</strong>' if label.startswith('All ') else name}</td>"
            f"<td>{len(group):,}</td>"
            + cells(score_texts)
            + cells([EMPTY if a is None else f"{a:.2f}" for a in acc])
            + cells([EMPTY if c is None else money(c * 1000) for c in cost])
            + f'<td class="group">{ratio_text(cost[1], cost[0])}</td>'
            "</tr>"
        )
    out += ["</tbody>", "</table>", ""]
    return out


def strata_table(
    ab: dict[str, Any],
    reg: dict[str, Any],
    results: dict[str, Any],
    sources: list[str],
    shown: list[str],
) -> list[str]:
    """One table of a pool's documents banded by size, for some of the values.

    Rows are the `strata` bands and a total; for each value, the mean per-document
    `score`, weighted accuracy and cost per 1,000 documents over the band's documents
    (only documents every value here scored); and, for each model with both modes,
    what agentic costs against single-pass. The best score in each row is bold. A
    document a value has no score for is named in a note rather than dropped silently.
    """
    spec = ab["strata"]
    datasets: dict[str, Any] = reg["datasets"]
    per_doc = results.get("per_doc") or {}
    sample = results.get("sample") or {}
    models = results.get("models") or {}
    by, score = str(spec["by"]), str(spec.get("score") or ab["by_size"]["metric"])
    bands = [int(b) for b in spec["bands"]]
    noun = str(spec.get("noun") or "rows")
    things = str(spec.get("documents") or "documents")
    thing = things.removesuffix("s")
    values, docs = strata_docs(sources, shown, per_doc, sample, score, by)
    if not docs or not values:
        return []
    modes = value_modes(models, sources, values)
    labels = {v: mode_label(v, modes) for v in values}
    pairs: list[tuple[str, str, str]] = []
    for model in dict.fromkeys(modes[v][0] for v in values if v in modes):
        single = next((v for v in values if modes.get(v) == (model, MODES[0])), None)
        agent = next((v for v in values if modes.get(v) == (model, MODES[1])), None)
        if single and agent:
            pairs.append((model, single, agent))
    score_name = noun.removesuffix("s").replace(" ", "-") + " score"

    def found(group: list[str], value: str, metric: str) -> list[float]:
        return [
            float(e[metric])
            for doc in group
            if metric
            in (e := (per_doc.get(f"{docs[doc][0]}|{value}") or {}).get(doc) or {})
        ]

    def means(group: list[str], value: str, metric: str) -> float | None:
        scores = found(group, value, metric)
        return statistics.fmean(scores) if scores else None

    groups = [
        [d for d, (_, x) in docs.items() if band_of(x, bands) == i]
        for i in range(len(bands))
    ]
    rows = list(zip(band_labels(bands, noun), groups, strict=True))
    rows.append((f"All {things}", sorted(docs)))
    paired = {v for _, single, agent in pairs for v in (single, agent)}
    if pairs and paired == set(values):
        out = []
        for model, single, agent in pairs:
            out += strata_html(
                model,
                single,
                agent,
                rows,
                found,
                score,
                score_name,
                noun,
                things,
                {d: x for d, (_, x) in docs.items()},
            )
    else:
        out = strata_markdown(
            values, labels, pairs, rows, means, found, score, score_name, noun, things
        )
    out.append("")

    used = sorted({k for k, _ in docs.values()}, key=sources.index)
    runs = span(
        [
            int((sample.get(f"{k}|{v}") or {}).get("runs") or 0)
            for k in used
            for v in values
            if (sample.get(f"{k}|{v}") or {}).get("runs")
        ]
        or [ab.get("runs", 0)]
    )
    basis = results.get("cost_basis") or {}
    cold = any(
        (basis.get(f"{k}|{v}") or {}).get("mode") == "cold-run"
        for k in used
        for v in values
    )
    bold_rule = (
        f"No {score_name} for {things} with no {noun}. Bold: the better mode, where "
        "the gap exceeds twice its standard error."
        if pairs and paired == set(values)
        else f"A bold {score_name} beats every other in its row by more than twice the "
        "combined standard error."
    )
    note = (
        f"*{things[:1].upper() + things[1:]} grouped by the {noun} their ground truth "
        f"holds. Each score and cost is a mean over the row's {things}; only {things} "
        "every run of every value scored are counted, each once. The "
        f"{score_name} is the share of rows matched: matched rows over the larger "
        "of the true and returned row counts. Cost is each "
        f"{thing}'s own Amazon Bedrock and Amazon Textract metering, without the "
        f"{things} left out, so it can be lower than the run cost in the table above"
        + (
            ", from each value's first, cold-cache run"
            if cold
            else ", averaged over runs that share no prompt cache"
        )
        + "; Agentic costs is agentic's cost over single-pass's for the same model. "
        + bold_rule
        + "* Datasets: "
        + ", ".join(dataset_label(k, datasets, short=True) for k in used)
        + f". N = {len(docs)} {things} × {runs} runs."
    )
    out += [note, ""]

    # Documents a value has no score for: counted, named by size, never hidden. One
    # whose every run COMPLETED was extracted but not scored, because Test Studio's
    # evaluation step timed out; any other failed in the pipeline.
    unfinished = results.get("unfinished") or {}
    for v in values:
        late: dict[str, dict[str, Any]] = {}
        for k in sources:
            for doc, entry in (unfinished.get(f"{k}|{v}") or {}).items():
                late.setdefault(doc, entry)
        if not late:
            continue
        timed_out = {
            d: e for d, e in late.items() if set(e.get("status") or []) <= {"COMPLETED"}
        }
        failed = {d: e for d, e in late.items() if d not in timed_out}

        def described(group: dict[str, dict[str, Any]]) -> str:
            parts = []
            for _, e in sorted(
                group.items(), key=lambda de: de[1].get("list_rows", -1)
            ):
                size = (
                    f"{int(e['list_rows']):,} {noun}"
                    if "list_rows" in e
                    else "size unknown"
                )
                parts.append(f"{size}, {e['runs']} of {e['of']} runs")
            return "; ".join(parts)

        if timed_out:
            n = len(timed_out)
            out += [
                f"Test Studio could not score {n} {thing if n == 1 else things} for "
                f"{labels[v]}: its evaluation step timed out, although extraction "
                f"finished ({described(timed_out)}). Left out of every column.",
                "",
            ]
        if failed:
            n = len(failed)
            out += [
                f"{labels[v]} failed on {n} {thing if n == 1 else things} "
                f"({described(failed)}). "
                f"{'It is' if n == 1 else 'They are'} left out of every column.",
                "",
            ]
    return out


def value_cell(value: str, ab: dict[str, Any]) -> str:
    """A value as a results table names it: the `reference` value bold, and marked
    "(baseline)" or "(recommended)" unless its name already carries a note in
    parentheses."""
    text = VALUE_NAMES.get(value, value)
    if value != ab.get("reference"):
        return text
    mark = "baseline" if ab.get("baseline") else "recommended"
    return f"**{text}**" if text.endswith(")") else f"**{text} ({mark})**"


def heading_of(ab: dict[str, Any]) -> str:
    """The section heading of an ablation: its `heading`, else its question."""
    return str(ab.get("heading") or ab["question"])


def reference_setup(ab: dict[str, Any]) -> str:
    """The setup the `reference` value stands for, as a link: the baseline setup the
    ablation changes one setting from, or else the recommended setup."""
    on_index = ab["page"] == "index"
    if ab.get("baseline"):
        return f"[baseline setup]({'#baseline-setup' if on_index else PRESETS})"
    return "reference configuration"


def reference_note(ab: dict[str, Any]) -> str:
    """The sentence under a results table saying what the bold name is."""
    if not ab.get("reference"):
        return ""
    if ab.get("baseline"):
        return f"The bold name is the value the {reference_setup(ab)} uses."
    return f"The bold name is the {reference_setup(ab)}."


RUN_LIST = re.compile(r"^Test Studio runs ([^(]*?)\s*(\(.*\))?$")


def source_text(ab: dict[str, Any], results: dict[str, Any]) -> str:
    """Where the results come from: a list of Test Studio runs is counted, and its
    run IDs left to the results file."""
    source = str(results.get("source") or "")
    m = RUN_LIST.match(source)
    if not m:
        return f"`{source}`"
    runs = [r for r in m.group(1).split(", ") if r]
    tail = f" {m.group(2)}" if m.group(2) else ""
    return (
        f"{len(runs)} Test Studio runs{tail}, listed in "
        f"`benchmarks/results/guidance/{ab['id']}.json`"
    )


class Rendered:
    """One ablation's FAQ entry, details section and plots."""

    def __init__(self) -> None:
        self.faq: list[str] = []
        self.details: list[str] = []
        self.plots: dict[str, str] = {}


def render_ablation(
    ab: dict[str, Any], reg: dict[str, Any], results: dict[str, Any] | None
) -> Rendered:
    datasets: dict[str, Any] = reg["datasets"]
    metrics: dict[str, Any] = reg["metrics"]
    presets = PRESETS if ab["page"] != "index" else "#baseline-setup"
    cells: dict[str, Cell] = dict((results or {}).get("cells", {}))
    usage = (results or {}).get("usage") or {}
    models = (results or {}).get("models") or {}
    sample = (results or {}).get("sample") or {}
    region = str((results or {}).get("region") or "")
    costs, unpriced, cold = price_usage(
        usage,
        (results or {}).get("cost_basis") or {},
        long_context_surcharges(results),
    )
    for key, names in unpriced.items():
        for name in sorted(names):
            print(
                f"warning: {ab['id']} {key}: no price for {name} in "
                f"{PRICING.relative_to(REPO)}; counted as $0",
                file=sys.stderr,
            )
    cells.update({f"{key}|{COST}": cell for key, cell in costs.items()})
    # The cost columns the tables show, each noted once under them.
    shows_cost: list[str] = []
    shows_cold = False
    out = Rendered()
    answer = results["verdict"] if results else "Not yet measured."

    # Details, collapsed under the answer: what was run, then the tables.
    d = out.details
    on_preset = bool(results) and (
        bool(ab.get("on_preset") or ab.get("interim")) or not ab.get("baseline")
    )
    if on_preset:
        d += [INTERIM_NOTE, ""]
    if results:
        d += [
            f"*Measured on release {results['release']}, {results['measured']}, "
            f"{results['region']}. Source: {source_text(ab, results)}.*",
            "",
        ]
    if ab.get("notes"):
        d += [str(ab["notes"]).strip(), ""]
    new_sets = [datasets[k]["name"] for k in ab["datasets"] if datasets[k].get("new")]
    if new_sets:
        d += ["Needs a dataset we haven't built yet: " + "; ".join(new_sets) + ".", ""]
    held = ab.get("holds")
    base = reg.get("baselines", {}).get(ab["baseline"]) if ab.get("baseline") else None
    if base:
        # The baseline is described once, on the index page.
        setup = reference_setup(ab)
        fixed_text = f"{held}; the rest as in the {setup}" if held else f"The {setup}"
    elif held:
        fixed_text = f"{held}; the rest as each dataset's [preset]({presets}) sets it"
    else:
        fixed_text = f"As each dataset's [preset]({presets}) sets it"
    if ab.get("derives"):
        runs_text = f"None: computed from the {ab['derives']} runs"
    else:
        runs_text = f"{ab['runs']} per document, per value"
    d += [
        "| | |",
        "|---|---|",
        f"| **Changes** | {ab['changes']} |",
        f"| **Everything else** | {fixed_text} |",
        "| **Datasets** | "
        + "; ".join(dataset_label(k, datasets) for k in ab["datasets"])
        + " |",
    ]
    spec_models = models_sentence(
        model_steps(models, ab["datasets"], ab["values"]), ab["datasets"], datasets
    )
    if spec_models:
        d.append(f"| **Models** | {spec_models.removeprefix('Models: ')} |")
    spec_sizes = sample_sizes(sample, ab["datasets"], ab["values"])
    spec_n = sample_sentence(spec_sizes, ab["datasets"], datasets)
    if spec_n:
        runs_text += f"; {spec_n.rstrip('.')}"
    d += [f"| **Runs** | {runs_text} |", ""]

    # One plot and one table per dataset kind. The first plot drawn is the
    # headline and goes in the FAQ entry; any others stay with their tables.
    kinds: list[str] = []
    for ds_key in ab["datasets"]:
        if datasets[ds_key]["kind"] not in kinds:
            kinds.append(datasets[ds_key]["kind"])
    headline: list[str] = []
    # One chart per question, under its heading. Where a by-size chart answers the
    # question, every other chart goes to the details.
    by_size_first = bool(results and ab.get("by_size") and results.get("per_doc"))
    charts_in_details = by_size_first
    drawn_any = False
    too_easy: list[str] = []
    for kind in kinds:
        cols = [m for m in ab["metrics"] if kind in metrics[m]["kinds"]]
        if not cols:
            continue
        wanted = ab.get("plot") or []
        wanted = [wanted] if isinstance(wanted, str) else list(wanted)
        plot_metrics = [m for m in wanted if m in cols] or [cols[0]]
        plot_metric = plot_metrics[0]
        kind_sets = [k for k in ab["datasets"] if datasets[k]["kind"] == kind]
        cost_col = cost_column(ab, kind, metrics)
        # The `dataset|value`s that have the cost shown.
        priced_keys = {
            f"{k}|{v}"
            for k in kind_sets
            for v in ab["values"]
            if (
                f"{k}|{v}" in costs
                if cost_col == COST
                else isinstance(cells.get(f"{k}|{v}|{cost_col}"), dict)
            )
        }
        priced = (
            bool(usage) and kind in metrics[COST]["kinds"]
            if cost_col == COST
            else bool(priced_keys)
        )
        if cost_col != COST and results and not priced:
            print(
                f"warning: {ab['id']}: cost_metric {cost_col} has no cells in its "
                "results file; re-collect it",
                file=sys.stderr,
            )
        if priced:
            cols = with_cost_column(cols, cost_col)
        # The ceiling is weighted accuracy's, so it says nothing where that is not
        # shown: a list's completeness of 1.0 is the finding, not a dataset too easy.
        ceiling, rows_only = (
            ceiling_sets(ab, cells, kind_sets, plot_metrics)
            if "weighted_accuracy" in cols
            else ([], set())
        )
        too_easy += [
            f"*{datasets[k].get('short', datasets[k]['name'])}: every value scored "
            f"{CEILING:.2f} or higher, too easy to separate.*"
            for k in ceiling
        ]
        chart_sets = [k for k in kind_sets if k not in ceiling]
        unit = per_unit(kind)
        steps = model_steps(models, kind_sets, ab["values"])
        sizes = sample_sizes(sample, kind_sets, ab["values"])
        panels = []
        for ds_key in chart_sets:
            panel_metric = ALL_RIGHT if ds_key in rows_only else plot_metric
            rows = []
            for value in ab["values"]:
                cell = cells.get(f"{ds_key}|{value}|{panel_metric}")
                rows.append((value, cell if isinstance(cell, dict) else None))
            panels.append(
                (
                    datasets[ds_key].get("short", datasets[ds_key]["name"]),
                    panel_subtitle(
                        steps.get(ds_key, {}),
                        sizes.get(ds_key),
                        lead_line(ab["page"], models, ds_key, ab["values"]),
                        unit,
                    ),
                    rows,
                )
            )
        panels = [p for p in panels if any(cell for _, cell in p[2])]
        measured = [
            f"{ds_key}|{value}"
            for ds_key in chart_sets
            for value in ab["values"]
            if any(
                isinstance(cells.get(f"{ds_key}|{value}|{m}"), dict)
                for m in plot_metrics
            )
        ]
        use_cost = (
            plot_metric != cost_col
            and bool(measured)
            and all(isinstance(cells.get(f"{k}|{cost_col}"), dict) for k in measured)
        )
        x_metric = (
            (cost_col if use_cost else None)
            or ab.get("plot_x")
            or (
                "total_tokens"
                if "total_tokens" in cols
                else next(
                    (m for m in cols if m.endswith("_tokens") and m != plot_metric),
                    None,
                )
            )
        )
        # The marginal cost of error applies to an accuracy against cost. The table's
        # column carries it for the first plotted metric it applies to.
        mcoe_metrics = (
            {m for m in plot_metrics if m in ERROR_RATE_METRICS}
            if x_metric == cost_col
            else set()
        )
        table_mcoe = next((m for m in plot_metrics if m in mcoe_metrics), None)
        mcoe: dict[str, tuple[str, dict[str, str]]] = {}
        scatter: list[tuple[str, list[Any], dict[str, tuple[float, str]]]] = []
        baseline_specs: dict[str, dict[str, Any]] = {}
        if x_metric and not plot_metric.endswith("_tokens"):
            for ds_key in chart_sets:
                pts = []
                for value in ab["values"]:
                    x = cells.get(f"{ds_key}|{value}|{x_metric}")
                    ys = {
                        m: y
                        for m in plot_metrics
                        if (ds_key not in rows_only or m == ALL_RIGHT)
                        and isinstance(y := cells.get(f"{ds_key}|{value}|{m}"), dict)
                    }
                    if isinstance(x, dict) and ys:
                        pts.append((value, x, ys))
                if not pts:
                    continue
                baselines: dict[str, tuple[float, str]] = {}
                for m in plot_metrics:
                    spec = metrics[m].get("baseline")
                    if not spec:
                        continue
                    found = (
                        [float(spec["value"])]
                        if "value" in spec
                        else [
                            cell["mean"]
                            for value in ab["values"]
                            if isinstance(
                                cell := cells.get(f"{ds_key}|{value}|{spec['metric']}"),
                                dict,
                            )
                        ]
                    )
                    if found and any(m in ys for _, _, ys in pts):
                        baselines[m] = (statistics.fmean(found), spec["name"])
                        baseline_specs[m] = spec
                scatter.append((ds_key, pts, baselines))
                if table_mcoe and any(table_mcoe in ys for _, _, ys in pts):
                    n_units = units_per_doc(
                        results or {}, error_unit(kind, table_mcoe), ds_key
                    )
                    if n_units is None:
                        print(
                            f"warning: {ab['id']} {ds_key}: no "
                            f"{PER_DOC_COUNTS[error_unit(kind, table_mcoe)]} in its "
                            "results file, so no cost per error prevented; re-collect it",
                            file=sys.stderr,
                        )
                    else:
                        mcoe[ds_key] = mcoe_cells(
                            [
                                (v, x, ys[table_mcoe])
                                for v, x, ys in pts
                                if table_mcoe in ys
                            ],
                            n_units,
                        )
        if panels:
            name = f"{ab['id']}-{kind}.svg"
            metric_name = metrics[plot_metric]["name"]
            on_cost = x_metric == cost_col or plot_metric == cost_col
            prices = price_caveat_svg(region) if on_cost else ""
            # A step's cost axis names the step; the whole pipeline's is the chart's
            # default.
            cost_axis = (
                cost_axis_title(cost_col, metrics, unit) if x_metric == cost_col else ""
            )
            # The first chart carries the takeaway. Under a by-size chart, which
            # carries it already, it carries its own (`details_takeaway`).
            title = (
                str(
                    ab.get("details_takeaway" if by_size_first else "takeaway")
                    or ab["question"]
                )
                if not drawn_any
                else ab["question"]
            )
            drawn_any = True
            if scatter and x_metric:
                x_name = metrics[x_metric]["name"]
                drawn = [
                    m
                    for m in plot_metrics
                    if any(m in ys for _, pts, _ in scatter for _, _, ys in pts)
                ]
                metric_name = " and ".join(
                    metrics[m]["name"] if i == 0 else lower_name(metrics[m]["name"])
                    for i, m in enumerate(drawn)
                )
                shown_keys = [k for k, _, _ in scatter]
                fixed = {k: fixed_model(ab, models, k) for k in shown_keys}
                same_fixed = len(set(fixed.values())) == 1
                varies = f"Varies: {varies_text(ab)}"
                if same_fixed and fixed[shown_keys[0]]:
                    varies += f" · Fixed: {fixed[shown_keys[0]]}"
                if len(shown_keys) == 1:
                    k = shown_keys[0]
                    lead = chart_name(k, datasets)
                    if k in sizes:
                        lead += f", {n_chart(sizes[k])}"
                    subtitle = f"{lead} · {varies}"
                else:
                    subtitle = varies
                groups = [
                    (
                        chart_name(k, datasets)
                        + (f", {n_chart(sizes[k])}" if k in sizes else ""),
                        "" if same_fixed or not fixed[k] else f"Fixed: {fixed[k]}",
                        pts,
                        baselines,
                    )
                    for k, pts, baselines in scatter
                ]
                svg = draw_scatter(
                    x_name,
                    [
                        (
                            m,
                            metrics[m]["name"],
                            axis_title(m, metrics),
                            m in SHARE_METRICS,
                        )
                        for m in drawn
                    ],
                    groups,
                    ab.get("runs", 0),
                    title=title,
                    subtitle=subtitle,
                    labels={v: short_label(v, ab) for v in ab["values"]},
                    default=default_value(ab),
                    x_money=x_metric == cost_col,
                    unit=unit,
                    mcoe=mcoe_metrics,
                    prices=prices,
                    families=bool(ab.get("families")),
                    cost_axis=cost_axis,
                    series=bool(ab.get("series")),
                    not_sent={
                        gi: [
                            v
                            for v in ab["values"]
                            if f"{k}|{v}" in ((results or {}).get("not_sent") or {})
                        ]
                        for gi, (k, _, _) in enumerate(scatter)
                    },
                )
                out.plots[name] = svg
                x_words = (
                    f"cost per 1,000 {unit}"
                    if x_metric == COST
                    else lower_first(cost_axis.removesuffix(" ($)"))
                    if cost_axis
                    else lower_first(x_name)
                )
                alt = (
                    f"{title}: {lower_name(metric_name)} against {x_words}, one point "
                    f"per {varies_text(ab)}"
                )
                caption = f"*{BANDS_CAPTION}"
                if 'class="mcoe"' in svg:
                    caption += MCOE_CAPTION
                marks = [
                    f"{spec['name']} ({spec['caption']})"
                    for spec in baseline_specs.values()
                ]
                if marks:
                    caption += (
                        f" Dashed lines mark {' and '.join(marks)}."
                        if len(marks) > 1
                        else f" The dashed line marks {marks[0]}."
                    )
                if x_axes_differ(
                    [[x["mean"] for _, x, _ in pts] for _, pts, _ in scatter]
                ):
                    caption += DIFFER_CAPTION
                caption += "*"
            else:
                out.plots[name] = draw_plot(
                    metric_name,
                    panels,
                    ab.get("runs", 0),
                    prices=prices,
                    ratio=plot_metric in RATIO_METRICS,
                )
                alt = f"{metric_name} for each value of {ab['changes']}"
                caption = f"*{CAPTION}*"
            plot_md = [f"![{alt}]({PLOTS_FROM_PAGES}/{name})", "", caption, ""]
            if headline or charts_in_details:
                d += plot_md
            else:
                headline = plot_md
        # Datasets with no measured cell in this kind get no rows, and a kind with
        # none at all gets no table.
        table_sets = [
            k
            for k in kind_sets
            if any(
                cells.get(f"{k}|{v}|{m}") is not None
                for v in ab["values"]
                for m in cols
            )
        ]
        if not table_sets or ab.get("grid"):
            # A grid's own tables (render_grid) carry its results.
            continue
        header = ["Dataset", ab["changes"]] + [
            unit_name(metrics[m]["name"], unit) for m in cols
        ]
        refs = {ref for ref, _ in mcoe.values()}
        if mcoe:
            versus = short_label(next(iter(refs)), ab) if len(refs) == 1 else "cheapest"
            # A packet's section or page accuracy prices one wrong section or page.
            err = error_unit(kind, str(table_mcoe))
            what = f"wrong {err}" if err in PER_DOC_COUNTS else "error"
            header.append(
                f"[{cost_words(cost_col, metrics)} per {what} prevented]({MCOE_PAGE}) "
                f"vs {versus}"
            )
        d.append("| " + " | ".join(header) + " |")
        d.append("|" + "---|" * len(header))
        any_bold = False
        for ds_key in table_sets:
            _, prices_for = mcoe.get(ds_key, ("", {}))
            best = {
                m: winner({v: cells.get(f"{ds_key}|{v}|{m}") for v in ab["values"]}, m)
                for m in cols
            }
            for value in ab["values"]:
                row = [
                    datasets[ds_key].get("short", datasets[ds_key]["name"]),
                    value_cell(value, ab),
                ]
                for m in cols:
                    on = best[m] == value
                    any_bold = any_bold or on
                    row.append(
                        bold(cell_text(cells.get(f"{ds_key}|{value}|{m}"), m), on)
                    )
                if mcoe:
                    row.append(prices_for.get(value, EMPTY))
                d.append("| " + " | ".join(row) + " |")
        d.append("")
        failed = failures_line(sample, table_sets, ab["values"], ab, datasets)
        if failed:
            d += [failed, ""]
        refused = not_sent_line(
            (results or {}).get("not_sent") or {}, table_sets, ab["values"], datasets
        )
        if refused:
            d += [refused, ""]
        notes = [BOLD_FIGURE] if any_bold else []
        if any(value_cell(v, ab) != VALUE_NAMES.get(v, v) for v in ab["values"]):
            notes.append(reference_note(ab))
        if notes:
            d += ["*" + " ".join(notes) + "*", ""]
        if cost_col in cols and priced_keys:
            if cost_col not in shows_cost:
                shows_cost.append(cost_col)
            shows_cold = shows_cold or any(
                f"{k}|{v}" in cold for k in kind_sets for v in ab["values"]
            )
            missing = sorted(
                {
                    name
                    for k in kind_sets
                    for v in ab["values"]
                    for name in unpriced.get(f"{k}|{v}", ())
                }
            )
            if missing:
                d += ["Not priced: " + ", ".join(f"`{n}`" for n in missing) + ".", ""]
    if shows_cost:
        for metric in shows_cost:
            d += [cost_note(region, str(metrics[metric].get("note") or "")), ""]
        if shows_cold:
            d += [COLD_NOTE, ""]
    d.append("")

    # FAQ entry: the question, its chart, the answer, and the details collapsed.
    if results:
        charts, tables, by_size_plots = render_by_size(ab, reg, results)
        out.plots.update(by_size_plots)
        if charts:
            headline = charts[0]
            tables = [line for chart in charts[1:] for line in chart] + tables
        if ab.get("grid"):
            # The grid's tables answer the question, so they come first.
            tables = render_grid(ab, reg, cells, region, sample) + tables
        # Under the settings table, before the per-kind tables.
        at = next(i for i, line in enumerate(d) if line.startswith("| **Runs**")) + 2
        d[at:at] = tables
    out.faq += [f"### {heading_of(ab)}", "", *headline]
    if too_easy:
        out.faq += [" ".join(too_easy), ""]
    out.faq += [f"**Finding:** {answer}", ""]
    if results:
        out.faq += render_econ(ab, reg, results, cells)
        while d and d[-1] == "":
            d.pop()
        out.faq += [
            "<details>",
            "<summary>Details</summary>",
            "",
            *d,
            "",
            "</details>",
            "",
        ]
    return out


def render_feature(ab: dict[str, Any]) -> Rendered:
    """A `feature` ablation's FAQ entry: its question, so its anchor stays, and one
    line linking to the section on the page that answers it. No plots."""
    heading = ab["feature"]
    if not isinstance(heading, str) or not heading.strip():
        raise SystemExit(f"{ab['id']}: feature must be the heading of its section")
    out = Rendered()
    out.faq += [
        f"### {heading_of(ab)}",
        "",
        f"See [{heading}](#{slugify(heading)}).",
        "",
    ]
    return out


def render_entry(
    ab: dict[str, Any], reg: dict[str, Any], results: dict[str, Any] | None
) -> Rendered:
    """An ablation's FAQ entry and plots: a feature's link, else render_ablation."""
    return (
        render_feature(ab) if ab.get("feature") else render_ablation(ab, reg, results)
    )


def grid_number(v: float, metric: str) -> str:
    """A figure in a grid table: dollars for a cost, "2.5×" for a ratio, else 0.81."""
    if is_cost_metric(metric):
        return money(v)
    return times(v) if metric in RATIO_METRICS else f"{v:.2f}"


def render_grid(
    ab: dict[str, Any],
    reg: dict[str, Any],
    cells: dict[str, Cell],
    region: str,
    sample: dict[str, dict[str, Any]],
) -> list[str]:
    """One small table per metric, rows and columns from the ablation's grid.

    A value is bold where it beats the runner-up of its table by more than twice the
    combined standard error (winner): the highest score, or the lowest cost.
    """
    metrics: dict[str, Any] = reg["metrics"]
    grid = ab["grid"]
    rows, cols = grid["rows"], grid["cols"]
    shown = [m for m in ab["metrics"] if not m.endswith("_tokens")]
    plot = ab.get("plot")
    plot = plot[0] if isinstance(plot, list) else plot
    if plot in shown:
        shown.remove(plot)
        shown.insert(0, plot)
    out: list[str] = []
    any_bold = False
    # The cost columns shown, each noted once under the tables.
    priced: list[str] = []
    for ds_key in ab["datasets"]:
        unit = per_unit(reg["datasets"][ds_key]["kind"])
        cost_col = cost_column(ab, reg["datasets"][ds_key]["kind"], metrics)
        has_cost = any(
            isinstance(cells.get(f"{ds_key}|{r} · {c}|{cost_col}"), dict)
            for r in rows
            for c in cols
        )
        if has_cost and cost_col not in priced:
            priced.append(cost_col)
        for metric in shown + ([cost_col] if has_cost else []):
            values = {
                (r, c): cells.get(f"{ds_key}|{r} · {c}|{metric}")
                for r in rows
                for c in cols
            }
            means = {k: v["mean"] for k, v in values.items() if isinstance(v, dict)}
            if not means:
                continue
            best = winner(values, metric)
            name = unit_name(metrics[metric]["name"], unit)
            if len(ab["datasets"]) > 1:
                short = reg["datasets"][ds_key].get("short", ds_key)
                name = f"{short}: {lower_first(name)}"
            out += [f"**{name}**", "", "| | " + " | ".join(cols) + " |"]
            out.append("|---|" + "---|" * len(cols))
            for r in rows:
                row = [r]
                for c in cols:
                    m = means.get((r, c))
                    if m is None:
                        row.append(EMPTY)
                        continue
                    se = (values.get((r, c)) or {}).get("se")
                    text = grid_number(m, metric)
                    if isinstance(se, (int, float)) and se > 0:
                        text += f" ± {grid_number(se, metric)}"
                    any_bold = any_bold or best == (r, c)
                    row.append(bold(text, best == (r, c)))
                out.append("| " + " | ".join(row) + " |")
            out.append("")
    failed = failures_line(
        sample,
        ab["datasets"],
        [f"{r} · {c}" for r in rows for c in cols],
        ab,
        reg["datasets"],
    )
    if failed:
        out += [failed, ""]
    notes: list[str] = []
    ref = ab.get("reference")
    if ref and " · " in ref:
        row, col = ref.split(" · ", 1)
        notes.append(f"The {reference_setup(ab)} is *{row}* with *{col}*.")
    if any_bold:
        notes.append(
            "A bold figure beats every other in its table by more than twice the "
            "combined standard error."
        )
    if notes:
        out += ["*" + " ".join(notes) + "*", ""]
    for metric in priced:
        out += [cost_note(region, str(metrics[metric].get("note") or "")), ""]
    return out


def question_anchor(ab: dict[str, Any]) -> str:
    """Where a link to an ablation goes: its question once measured, else Planned."""
    measured = shown_results(ab) is not None
    return slugify(heading_of(ab) if measured else PLANNED)


#: A link whose text is an ablation ID: "[EX-02](#planned)", "[EX-02](extraction.md#x)".
ID_LINK = re.compile(r"\[([A-Z]{2}-\d{2})\]\((?:([\w-]+)\.md)?#[\w-]*\)")


def fix_id_links(text: str, page: str, reg: dict[str, Any]) -> str:
    """Point every ablation-ID link on a page at its question, or at Planned.

    The settings tables are written by hand and name the ablations that answer each
    setting; a link written before an ablation was measured goes to Planned, so it is
    moved to the question when the results arrive.
    """
    by_id = {ab["id"]: ab for ab in reg["ablations"]}

    def sub(m: re.Match[str]) -> str:
        ab = by_id.get(m.group(1))
        if ab is None:
            return m.group(0)
        target = "" if ab["page"] == page else f"{ab['page']}.md"
        return f"[{m.group(1)}]({target}#{question_anchor(ab)})"

    return ID_LINK.sub(sub, text)


def render_datasets(reg: dict[str, Any]) -> str:
    """The Benchmark Datasets page: one section per dataset in the registry."""
    datasets: dict[str, Any] = reg["datasets"]
    used: dict[str, list[dict[str, Any]]] = {k: [] for k in datasets}
    for ab in reg["ablations"]:
        for k in ab["datasets"]:
            used[k].append(ab)
    built = [k for k in datasets if not datasets[k].get("new")]
    planned = [k for k in datasets if datasets[k].get("new")]
    lines: list[str] = []
    for heading, keys in (
        ("Datasets we run", built),
        ("Planned datasets", planned),
    ):
        lines += [f"## {heading}", ""]
        for k in keys:
            ds = datasets[k]
            lines += [f"### {ds['name']}", ""]
            if ds.get("about"):
                lines.append(f"- **About the dataset:** {ds['about']}")
            elif ds.get("link"):
                lines.append(
                    f"- **About the dataset:** [{ds['name'].split(' (')[0].split(',')[0]}]({ds['link']})"
                )
            if ds.get("preset"):
                lines.append(f"- **Preset:** `{ds['preset']}`")
            if ds.get("testset"):
                lines.append(
                    f"- **Test Studio test set:** `{ds['testset']}`, "
                    f"{ds.get('docs', 20)} documents per run"
                )
            elif not ds.get("new"):
                lines.append(
                    "- **Test Studio test set:** not on the benchmark stack yet"
                )
            if used[k]:
                links = ", ".join(
                    f"[{ab['id']}]({ab['page']}.md#{question_anchor(ab)})"
                    + ("" if shown_results(ab) is not None else " (planned)")
                    for ab in used[k]
                )
                lines.append(f"- **Used by:** {links}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_page(
    page: str, reg: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """Return the page's generated regions and its plots."""
    faq: list[str] = []
    plots: dict[str, str] = {}
    planned: list[str] = []
    for ab in reg["ablations"]:
        if ab["page"] != page:
            continue
        results = shown_results(ab)
        if results is None:
            planned.append(f"- {heading_of(ab)} ({ab['id']})")
            continue
        r = render_entry(ab, reg, results)
        faq += r.faq
        plots.update(r.plots)
    if planned:
        faq += [f"### {PLANNED}", "", "We haven't run these yet.", "", *planned, ""]
    return {"FAQ": "\n".join(faq).rstrip() + "\n"}, plots


def splice(text: str, regions: dict[str, str], path: Path) -> str:
    for region in REGIONS:
        begin, end = markers(region)
        if begin not in text or end not in text:
            raise SystemExit(f"{path}: missing {begin} / {end} markers")
        head, rest = text.split(begin, 1)
        _, tail = rest.split(end, 1)
        text = f"{head}{begin}\n\n{regions[region]}\n{end}{tail}"
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="fail if a page is out of date"
    )
    parser.add_argument("--preview", metavar="ID", help="draw one ablation's plots")
    parser.add_argument("--results", type=Path, help="results file for --preview")
    parser.add_argument("--out", type=Path, help="output directory for --preview")
    args = parser.parse_args()

    reg = load_registry()
    ids = [ab["id"] for ab in reg["ablations"]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SystemExit(f"duplicate ablation IDs: {', '.join(duplicates)}")

    if args.preview:
        if not (args.results and args.out):
            raise SystemExit("--preview needs --results and --out")
        ab = next((a for a in reg["ablations"] if a["id"] == args.preview), None)
        if ab is None:
            raise SystemExit(f"no ablation {args.preview}")
        results = json.loads(args.results.read_text(encoding="utf-8"))
        rendered = render_entry(ab, reg, results)
        args.out.mkdir(parents=True, exist_ok=True)
        for name, svg in rendered.plots.items():
            (args.out / name).write_text(svg, encoding="utf-8")
            print(f"wrote {args.out / name}")
        # The FAQ entry as its page would show it, beside the plots.
        entry = args.out / f"{ab['id']}.md"
        entry.write_text("\n".join(rendered.faq).rstrip() + "\n", encoding="utf-8")
        print(f"wrote {entry}")
        return 0

    stale: list[str] = []
    expected: dict[str, str] = {}
    for page in sorted({ab["page"] for ab in reg["ablations"]}):
        path = PAGES / f"{page}.md"
        text = path.read_text(encoding="utf-8")
        regions, plots = render_page(page, reg)
        expected.update(plots)
        updated = splice(text, regions, path)
        if page != "index":  # index.md's hand-written text is edited by hand
            updated = fix_id_links(updated, page, reg)
        if updated != text:
            if args.check:
                stale.append(str(path.relative_to(REPO)))
            else:
                path.write_text(updated, encoding="utf-8")
                print(f"rendered {path.relative_to(REPO)}")
    ds_path = PAGES / f"{DATASETS_PAGE}"
    ds_text = ds_path.read_text(encoding="utf-8")
    begin, end = markers("DATASETS")
    head, rest = ds_text.split(begin, 1)
    _, tail = rest.split(end, 1)
    ds_updated = f"{head}{begin}\n\n{render_datasets(reg)}\n{end}{tail}"
    if ds_updated != ds_text:
        if args.check:
            stale.append(str(ds_path.relative_to(REPO)))
        else:
            ds_path.write_text(ds_updated, encoding="utf-8")
            print(f"rendered {ds_path.relative_to(REPO)}")
    existing = {p.name for p in PLOTS.glob("*.svg")} if PLOTS.exists() else set()
    for name, svg in expected.items():
        plot = PLOTS / name
        if not plot.exists() or plot.read_text(encoding="utf-8") != svg:
            if args.check:
                stale.append(str(plot.relative_to(REPO)))
            else:
                PLOTS.mkdir(parents=True, exist_ok=True)
                plot.write_text(svg, encoding="utf-8")
                print(f"drew {plot.relative_to(REPO)}")
    for name in sorted(existing - expected.keys()):
        if args.check:
            stale.append(f"{(PLOTS / name).relative_to(REPO)} (no longer drawn)")
        else:
            (PLOTS / name).unlink()
            print(f"removed {(PLOTS / name).relative_to(REPO)}")
    if stale:
        print(
            "out of date, run render_guidance.py: " + ", ".join(stale), file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
