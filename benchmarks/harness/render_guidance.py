#!/usr/bin/env python3
"""Render the ablation tables of the "Benchmarks by Problem Set" pages.

Reads the ablation registry (benchmarks/matrices/guidance_ablations.yaml) and writes three
regions of each page under docs/benchmark-guidance/, each between a pair of markers:

    <!-- BEGIN GENERATED FAQ -->       each question, its answer and its headline plot
    <!-- BEGIN GENERATED ABLATIONS --> each ablation's details and results tables

(each closed by the matching END marker), and the dataset list of datasets.md between
DATASETS markers. Text outside the
markers is hand-written and left alone. When a results file exists at
benchmarks/results/guidance/<ID>.json, its values fill the table; otherwise every cell is
"—" and the answer reads "Not yet measured". A results file looks like:

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
whenever at least one of its cells has a mean. `plot` may list several metrics: a
scatter then draws one chart per metric, side by side in the same image. A metric
with a `baseline` in the registry gets it as a dashed line, and an ablation marked
`families` colors its points by model family and gives each family its own chart.

An ablation with a `by_size` key also gets a chart of one per-document score against
the document's own size, drawn from the results file's `per_doc` block, which leads
its FAQ entry: a mean over a dataset says which value wins, and this says at what size
it starts winning.

`models` is optional: the model in force at each step of the pipeline. Every chart,
caption and details table states it, and a step whose model differs across the values
on the chart reads "varies (the setting under test)".

`sample` is optional: the documents each run scored and the number of runs, which
every chart, caption and details table states as "N = 20 documents x 3 runs". A
document that failed was not scored, so the caption says how many were left out.

`usage` is optional: each run's Bedrock and Textract usage per document. The renderer
prices it with config_library/pricing.yaml and adds a "Cost per 1,000 documents"
column to every table. `cost_basis` says which runs to price: by default only the
coldest run of each value, because repeat runs read the prompt cache the first run
wrote. When every plotted point has a cost, the plot puts cost on the x-axis, and when
the plotted metric is an accuracy, it draws the marginal cost of error from the
cheapest value to each more accurate one (marginal-cost-of-error.md).

Usage:
    python3 benchmarks/harness/render_guidance.py           # rewrite the pages
    python3 benchmarks/harness/render_guidance.py --check   # exit 1 if a page is stale
    python3 benchmarks/harness/render_guidance.py --preview EX-02 \
        --results sample.json --out /tmp/plots              # draw one ablation's plots
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import statistics
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from guidance_plots import (
    ERROR_RATE_METRICS,
    MAX_LABELLED,
    draw_by_size,
    draw_plot,
    draw_scatter,
    family,
    fmt,
    lower_name,
    marginal_cost_of_error,
    money,
)

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "benchmarks" / "matrices" / "guidance_ablations.yaml"
RESULTS = REPO / "benchmarks" / "results" / "guidance"
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
    " Lines run from the cheapest value to each more accurate one, labeled with the "
    "marginal cost of error: the extra cost per error prevented "
    f"([Marginal Cost of Error]({MCOE_PAGE}))."
)
FAMILY_CAPTION = " Color and shape mark the model family"
FAMILY_SPLIT_CAPTION = (
    ", and each family has its own chart on the same axes, with the other families' "
    "values as gray dots"
)
PAGES = REPO / "docs" / "benchmark-guidance"
#: Generated regions of a page. ABLATIONS is required; the others are optional.
REGIONS = ("FAQ", "ABLATIONS")
PRESETS = "index.md#how-we-run-a-benchmark"
DATASETS_PAGE = "datasets.md"
PLOTS = REPO / "images" / "benchmark-guidance"
PLOTS_FROM_PAGES = "../../images/benchmark-guidance"
EMPTY = "—"
CAPTION = (
    "Dots are the mean of the runs; shaded bands span two standard errors either side."
)
SCATTER_CAPTION = (
    "Each point is one value of the setting; shaded bands span two standard errors "
    "either side. Up and to the left is better."
)
#: Pipeline steps named on every chart, in order.
MODEL_STEPS = ("OCR", "Classification", "Extraction", "Confidence")
VARIES = "varies (the setting under test)"
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


def load_registry() -> dict[str, Any]:
    with REGISTRY.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_results(ablation_id: str) -> dict[str, Any] | None:
    path = RESULTS / f"{ablation_id}.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


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


def cost_note(region: str) -> str:
    return (
        f"*{price_caveat(region)} Amazon Bedrock and Amazon Textract only; Lambda and "
        "other AWS charges are not included.*"
    )


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


def price_usage(
    usage: dict[str, list[dict[str, float]]],
    basis: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, float]], dict[str, set[str]], set[str]]:
    """Cost per 1,000 documents for each `dataset|value`, and its unpriced usage.

    Each run's usage is priced on its own. `basis` says which of them count: a value
    whose runs could share a prompt cache is priced from its coldest run alone, with
    no standard error, and is named in the returned set so the page can footnote it.
    Usage with no price counts as $0 and is returned so the page can say so.
    """
    costs: dict[str, dict[str, float]] = {}
    unpriced: dict[str, set[str]] = {}
    cold: set[str] = set()
    for key, runs in usage.items():
        per_run = []
        for run in runs:
            total = 0.0
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
    so it reads "varies" rather than naming one of them.
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
        if seen:
            out[ds_key] = {
                step: texts[0] if len(set(texts)) == 1 else VARIES
                for step, texts in seen.items()
            }
    return out


def models_text(steps: dict[str, str]) -> str:
    return " · ".join(f"{s}: {steps[s]}" for s in MODEL_STEPS if s in steps)


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
                f"{step} by dataset: "
                + "; ".join(
                    f"{datasets[k].get('short', datasets[k]['name'])} — "
                    f"{steps[k].get(step, EMPTY)}"
                    for k in present
                )
            )
        else:
            common.append(f"{step}: {texts[0]}")
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


def n_short(entry: tuple[str, str, int]) -> str:
    docs, runs, _ = entry
    return f"{docs} documents × {runs} runs"


def n_full(entry: tuple[str, str, int]) -> str:
    """The sample size, and what a failed document took out of it."""
    failed = entry[2]
    if not failed:
        return n_short(entry)
    word = "document" if failed == 1 else "documents"
    return f"{n_short(entry)}, {failed} failed {word} per run left out"


def sample_sentence(
    sizes: dict[str, tuple[str, str, int]],
    ds_keys: list[str],
    datasets: dict[str, Any],
) -> str:
    """ "N = ..." for a caption or a table row, per dataset when they differ."""
    present = [k for k in ds_keys if k in sizes]
    if not present:
        return ""
    texts = {k: n_full(sizes[k]) for k in present}
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
    }
    if not texts:
        return ""
    if len(texts) > 1:
        return f"{label}: varies — see the key"
    return f"{label}: {texts.pop()}"


def panel_subtitle(
    steps: dict[str, str], size: tuple[str, str, int] | None, lead: str = ""
) -> tuple[str, str]:
    """The lines under a panel title: the page's own model, then every step and N."""
    parts = [models_text(steps)] if steps else []
    if size:
        parts.append(f"N = {n_short(size)}")
    return lead, " · ".join(p for p in parts if p)


def cell_text(cell: Cell | None, metric: str = "") -> str:
    if cell is None:
        return EMPTY
    if isinstance(cell, str):
        return cell
    if metric == COST:
        if not cell.get("se"):
            return money(cell["mean"])
        return f"{money(cell['mean'])} ± {money(2 * cell['se'])}"
    return f"{fmt(cell['mean'])} ± {fmt(2 * cell.get('se', 0))}"


def lower_first(text: str) -> str:
    """Lower-case the first letter only, so "Cost ... (USD)" keeps its acronym."""
    return text[:1].lower() + text[1:]


def with_cost(cols: list[str]) -> list[str]:
    """Add the cost column after the last tokens column, or at the end."""
    if COST in cols:
        return cols
    last = max((i for i, m in enumerate(cols) if m.endswith("_tokens")), default=-1)
    at = last + 1 if last >= 0 else len(cols)
    return [*cols[:at], COST, *cols[at:]]


def dataset_anchor(key: str, datasets: dict[str, Any]) -> str:
    return f"{DATASETS_PAGE}#{slugify(datasets[key]['name'])}"


def dataset_label(key: str, datasets: dict[str, Any], short: bool = False) -> str:
    ds = datasets[key]
    text = ds.get("short", ds["name"]) if short else ds["name"]
    return f"[{text}]({dataset_anchor(key, datasets)})"


LIST_SCORE_CAPTION = (
    "A list score is Stickler's score for the list: matched rows over the larger of "
    "the true and returned row counts."
)
BY_SIZE_CAPTION = (
    "One point per document per {changes}, on a log scale; the line is that value's "
    "running median over log-spaced bins of the x axis."
)


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


def render_by_size(
    ab: dict[str, Any], reg: dict[str, Any], results: dict[str, Any]
) -> tuple[list[str], dict[str, str]]:
    """The by-size chart of an ablation that has one, and its markdown.

    One chart per pool of datasets (dataset_pools), drawn from the results file's
    per-document scores: where a mean over a dataset says which value wins, this says
    at what size it starts winning.
    """
    spec = ab.get("by_size") or {}
    per_doc = results.get("per_doc") or {}
    if not spec or not per_doc:
        return [], {}
    datasets: dict[str, Any] = reg["datasets"]
    metrics: dict[str, Any] = reg["metrics"]
    metric, x_metric = str(spec["metric"]), str(spec["x"])
    models = results.get("models") or {}
    sample = results.get("sample") or {}
    md: list[str] = []
    plots: dict[str, str] = {}
    for pool in dataset_pools(ab["datasets"]):
        owner = by_size_owners(pool, ab["values"], per_doc, sample)
        series = by_size_series(owner, ab["values"], per_doc, metric, x_metric)
        if not series:
            continue
        used = sorted(set(owner.values()), key=pool.index)
        drawn = sorted({doc for _, pts in series for doc, _, _ in pts})
        steps = model_steps(models, used, ab["values"])
        runs = span(
            [
                int((sample.get(f"{k}|{v}") or {}).get("runs") or 0)
                for k in used
                for v in ab["values"]
                if (sample.get(f"{k}|{v}") or {}).get("runs")
            ]
            or [ab.get("runs", 0)]
        )
        n_text = f"N = {len(drawn)} documents × {runs} runs"
        y_name = metrics[metric]["name"]
        x_name = f"{metrics[x_metric]['name']}, log scale"
        name = f"{ab['id']}-by-size-{pool[0].replace('_', '-')}.svg"
        plots[name] = draw_by_size(
            x_name,
            y_name,
            series,
            title=" · ".join(
                datasets[k].get("short", datasets[k]["name"]) for k in used
            ),
            models=(
                lead_line(ab["page"], models, used[0], ab["values"]),
                " · ".join(
                    p for p in (models_text(steps.get(used[0], {})), n_text) if p
                ),
            ),
        )
        alt = (
            f"{y_name} against {lower_first(metrics[x_metric]['name'])} "
            f"for each {ab['changes']}"
        )
        caption = (
            f"*{y_name} per document, against {lower_first(metrics[x_metric]['name'])}. "
            + BY_SIZE_CAPTION.format(changes=lower_first(ab["changes"]))
            + (f" {LIST_SCORE_CAPTION}" if metric == "list_score" else "")
            + "*"
            + " Datasets: "
            + ", ".join(dataset_label(k, datasets, short=True) for k in used)
            + "."
        )
        plot_models = models_sentence(steps, used, datasets)
        if plot_models:
            caption += f" {plot_models}"
        caption += f" {n_text}."
        md += [f"![{alt}]({PLOTS_FROM_PAGES}/{name})", "", caption, ""]
    return md, plots


def details_heading(ab: dict[str, Any]) -> str:
    return f"{ab['id']}: {ab['question']}"


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
    presets = PRESETS if ab["page"] != "index" else "#how-we-run-a-benchmark"
    cells: dict[str, Cell] = dict((results or {}).get("cells", {}))
    usage = (results or {}).get("usage") or {}
    models = (results or {}).get("models") or {}
    sample = (results or {}).get("sample") or {}
    region = str((results or {}).get("region") or "")
    costs, unpriced, cold = price_usage(usage, (results or {}).get("cost_basis") or {})
    for key, names in unpriced.items():
        for name in sorted(names):
            print(
                f"warning: {ab['id']} {key}: no price for {name} in "
                f"{PRICING.relative_to(REPO)}; counted as $0",
                file=sys.stderr,
            )
    cells.update({f"{key}|{COST}": cell for key, cell in costs.items()})
    shows_cost = False
    shows_cold = False
    out = Rendered()
    answer = results["verdict"] if results else "Not yet measured."
    details_link = f"[Details: {ab['id']}](#{slugify(details_heading(ab))})"

    # Details: heading, answer, what was run.
    d = out.details
    d += [f"#### {details_heading(ab)}", "", f"**Answer:** {answer}"]
    if results:
        d += [
            "",
            f"*Measured on release {results['release']}, {results['measured']}, "
            f"{results['region']}. Source: `{results['source']}`.*",
        ]
    new_sets = [datasets[k]["name"] for k in ab["datasets"] if datasets[k].get("new")]
    if new_sets:
        d += ["", "Needs a dataset we haven't built yet: " + "; ".join(new_sets) + "."]
    held = ab.get("holds")
    fixed_text = (
        f"{held}; the rest as each dataset's [preset]({presets}) sets it"
        if held
        else f"As each dataset's [preset]({presets}) sets it"
    )
    if ab.get("derives"):
        runs_text = f"None: computed from the {ab['derives']} runs"
    else:
        runs_text = f"{ab['runs']} per document, per value"
    d += [
        "",
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
    for kind in kinds:
        cols = [m for m in ab["metrics"] if kind in metrics[m]["kinds"]]
        if not cols:
            continue
        wanted = ab.get("plot") or []
        wanted = [wanted] if isinstance(wanted, str) else list(wanted)
        plot_metrics = [m for m in wanted if m in cols] or [cols[0]]
        plot_metric = plot_metrics[0]
        if usage and kind in metrics[COST]["kinds"]:
            cols = with_cost(cols)
        kind_sets = [k for k in ab["datasets"] if datasets[k]["kind"] == kind]
        steps = model_steps(models, kind_sets, ab["values"])
        sizes = sample_sizes(sample, kind_sets, ab["values"])
        panels = []
        for ds_key in kind_sets:
            rows = []
            for value in ab["values"]:
                cell = cells.get(f"{ds_key}|{value}|{plot_metric}")
                rows.append((value, cell if isinstance(cell, dict) else None))
            panels.append(
                (
                    datasets[ds_key].get("short", datasets[ds_key]["name"]),
                    panel_subtitle(
                        steps.get(ds_key, {}),
                        sizes.get(ds_key),
                        lead_line(ab["page"], models, ds_key, ab["values"]),
                    ),
                    rows,
                )
            )
        panels = [p for p in panels if any(cell for _, cell in p[2])]
        measured = [
            f"{ds_key}|{value}"
            for ds_key in kind_sets
            for value in ab["values"]
            if any(
                isinstance(cells.get(f"{ds_key}|{value}|{m}"), dict)
                for m in plot_metrics
            )
        ]
        use_cost = (
            plot_metric != COST
            and bool(measured)
            and all(isinstance(cells.get(f"{k}|{COST}"), dict) for k in measured)
        )
        x_metric = (
            (COST if use_cost else None)
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
            if x_metric == COST
            else set()
        )
        table_mcoe = next((m for m in plot_metrics if m in mcoe_metrics), None)
        mcoe: dict[str, tuple[str, dict[str, float | None]]] = {}
        scatter = []
        baseline_specs: dict[str, dict[str, str]] = {}
        if x_metric and not plot_metric.endswith("_tokens"):
            for ds_key in kind_sets:
                pts = []
                for value in ab["values"]:
                    x = cells.get(f"{ds_key}|{value}|{x_metric}")
                    ys = {
                        m: y
                        for m in plot_metrics
                        if isinstance(y := cells.get(f"{ds_key}|{value}|{m}"), dict)
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
                    found = [
                        cell["mean"]
                        for value in ab["values"]
                        if isinstance(
                            cell := cells.get(f"{ds_key}|{value}|{spec['metric']}"),
                            dict,
                        )
                    ]
                    if found:
                        baselines[m] = (statistics.fmean(found), spec["name"])
                        baseline_specs[m] = spec
                scatter.append(
                    (
                        datasets[ds_key].get("short", datasets[ds_key]["name"]),
                        panel_subtitle(
                            steps.get(ds_key, {}),
                            sizes.get(ds_key),
                            lead_line(ab["page"], models, ds_key, ab["values"]),
                        ),
                        pts,
                        baselines,
                    )
                )
                if table_mcoe:
                    mcoe[ds_key] = marginal_cost_of_error(
                        [(v, x, ys[table_mcoe]) for v, x, ys in pts if table_mcoe in ys]
                    )
        plotted = [
            k
            for k in kind_sets
            if datasets[k].get("short", datasets[k]["name"]) in {p[0] for p in panels}
        ]
        if panels:
            name = f"{ab['id']}-{kind}.svg"
            metric_name = metrics[plot_metric]["name"]
            on_cost = x_metric == COST or plot_metric == COST
            prices = price_caveat_svg(region) if on_cost else ""
            if scatter and x_metric:
                x_name = metrics[x_metric]["name"]
                drawn = [
                    m
                    for m in plot_metrics
                    if any(m in ys for *_, pts, _ in scatter for _, _, ys in pts)
                ]
                metric_name = " and ".join(
                    metrics[m]["name"] if i == 0 else lower_name(metrics[m]["name"])
                    for i, m in enumerate(drawn)
                )
                out.plots[name] = draw_scatter(
                    x_name,
                    [
                        (
                            m,
                            metrics[m]["name"],
                            metrics[m].get("short", metrics[m]["name"]),
                        )
                        for m in drawn
                    ],
                    scatter,
                    ab.get("runs", 0),
                    x_money=x_metric == COST,
                    mcoe=mcoe_metrics,
                    prices=prices,
                    families=bool(ab.get("families")),
                )
                x_lower = lower_first(x_name)
                alt = f"{metric_name} against {x_lower} for each {ab['changes']}"
                caption = f"*{metric_name} against {x_lower}"
                if len(drawn) > 1:
                    caption += ", one chart each"
                caption += f". {SCATTER_CAPTION}"
                if ab.get("families"):
                    split = any(
                        len({family(v) for v, _, _ in pts}) > 1
                        for _, _, pts, _ in scatter
                    )
                    caption += FAMILY_CAPTION + (FAMILY_SPLIT_CAPTION if split else "")
                    caption += "."
                # A chart whose dots are numbered lists every value in its key.
                numbered = 'class="keyhead"' in out.plots[name]
                if (
                    not numbered
                    and max(len(p) for _, _, p, _ in scatter) > MAX_LABELLED
                ):
                    caption += (
                        " Labels mark the default and the values no other value "
                        "beats on both axes; hover a point for the rest."
                    )
                if any(gains for _, gains in mcoe.values()):
                    caption += MCOE_CAPTION
                for spec in baseline_specs.values():
                    caption += f" The dashed line is {spec['name']}: {spec['caption']}."
                caption += "*"
            else:
                out.plots[name] = draw_plot(
                    metric_name, panels, ab.get("runs", 0), prices=prices
                )
                alt = f"{metric_name} for each value of {ab['changes']}"
                caption = f"*{metric_name}. {CAPTION}*"
            caption += (
                " Datasets: "
                + ", ".join(dataset_label(k, datasets, short=True) for k in plotted)
                + "."
            )
            plot_models = models_sentence(steps, plotted, datasets)
            if plot_models:
                caption += f" {plot_models}"
            plot_n = sample_sentence(sizes, plotted, datasets)
            if plot_n:
                caption += f" {plot_n}"
            if on_cost:
                caption += f" {price_caveat(region)}"
            plot_md = [f"![{alt}]({PLOTS_FROM_PAGES}/{name})", "", caption, ""]
            if headline:
                d += plot_md
            else:
                headline = plot_md
        header = ["Dataset", ab["changes"]] + [metrics[m]["name"] for m in cols]
        refs = {ref for ref, _ in mcoe.values()}
        if mcoe:
            header.append(
                f"MCoE vs {next(iter(refs))}" if len(refs) == 1 else "MCoE vs cheapest"
            )
        d.append("| " + " | ".join(header) + " |")
        d.append("|" + "---|" * len(header))
        for ds_key in kind_sets:
            ref, gains = mcoe.get(ds_key, ("", {}))
            for value in ab["values"]:
                row = [datasets[ds_key].get("short", datasets[ds_key]["name"]), value]
                row += [cell_text(cells.get(f"{ds_key}|{value}|{m}"), m) for m in cols]
                if mcoe:
                    row.append(
                        EMPTY
                        if value not in gains
                        else "no measurable gain"
                        if gains[value] is None
                        else f"{money(gains[value] or 0.0)} per error prevented"
                    )
                d.append("| " + " | ".join(row) + " |")
        d.append("")
        if COST in cols and any(
            f"{k}|{v}" in costs for k in kind_sets for v in ab["values"]
        ):
            shows_cost = True
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
        d += [cost_note(region), ""]
        if shows_cold:
            d += [COLD_NOTE, ""]
    d.append("")

    # FAQ entry: the question, the answer, the headline plot, a link to details.
    out.faq += [f"### {ab['question']}", "", f"**Answer:** {answer} {details_link}", ""]
    if results:
        by_size, by_size_plots = render_by_size(ab, reg, results)
        out.faq += by_size
        out.plots.update(by_size_plots)
    if ab.get("grid") and results:
        # The grid's tables answer the question; the chart, when there is one,
        # follows them.
        out.faq += render_grid(ab, reg, cells, region, spec_n, spec_models)
        out.faq += headline
    else:
        out.faq += headline
    return out


def render_grid(
    ab: dict[str, Any],
    reg: dict[str, Any],
    cells: dict[str, Cell],
    region: str,
    sample_text: str,
    models_text: str,
) -> list[str]:
    """One small table per metric, rows and columns from the ablation's grid.

    The best value in each table is bold: the highest score, or the lowest cost.
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
    for ds_key in ab["datasets"]:
        has_cost = any(
            isinstance(cells.get(f"{ds_key}|{r} · {c}|{COST}"), dict)
            for r in rows
            for c in cols
        )
        for metric in shown + ([COST] if has_cost else []):
            values = {
                (r, c): cells.get(f"{ds_key}|{r} · {c}|{metric}")
                for r in rows
                for c in cols
            }
            means = {k: v["mean"] for k, v in values.items() if isinstance(v, dict)}
            if not means:
                continue
            best = (min if metric == COST else max)(means.values())
            name = (
                "Cost per 1,000 documents (USD)"
                if metric == COST
                else metrics[metric]["name"]
            )
            out += [f"**{name}**", "", "| | " + " | ".join(cols) + " |"]
            out.append("|---|" + "---|" * len(cols))
            for r in rows:
                row = [r]
                for c in cols:
                    m = means.get((r, c))
                    if m is None:
                        row.append(EMPTY)
                        continue
                    text = f"${m:,.0f}" if metric == COST else f"{m:.2f}"
                    row.append(f"**{text}**" if m == best else text)
                out.append("| " + " | ".join(row) + " |")
            out.append("")
    notes = [t for t in (sample_text, models_text) if t]
    if notes:
        out += ["*" + " ".join(notes) + " The best value in each table is bold.*", ""]
    if any(
        isinstance(cells.get(f"{k}|{r} · {c}|{COST}"), dict)
        for k in ab["datasets"]
        for r in rows
        for c in cols
    ):
        out += [cost_note(region), ""]
    return out


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
        ("Datasets we haven't built yet", planned),
    ):
        lines += [f"## {heading}", ""]
        for k in keys:
            ds = datasets[k]
            lines += [f"### {ds['name']}", ""]
            if ds.get("link"):
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
                    f"[{ab['id']}]({ab['page']}.md#{slugify(details_heading(ab))})"
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
    details: list[str] = []
    plots: dict[str, str] = {}
    for ab in reg["ablations"]:
        if ab["page"] == page:
            r = render_ablation(ab, reg, load_results(ab["id"]))
            faq += r.faq
            details += r.details
            plots.update(r.plots)
    regions = {
        "FAQ": "\n".join(faq).rstrip() + "\n",
        "ABLATIONS": "\n".join(details).rstrip() + "\n",
    }
    return regions, plots


def splice(text: str, regions: dict[str, str], path: Path) -> str:
    for region in REGIONS:
        begin, end = markers(region)
        if begin not in text or end not in text:
            if region == "ABLATIONS":
                raise SystemExit(f"{path}: missing {begin} / {end} markers")
            continue
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
        plots = render_ablation(ab, reg, results).plots
        args.out.mkdir(parents=True, exist_ok=True)
        for name, svg in plots.items():
            (args.out / name).write_text(svg, encoding="utf-8")
            print(f"wrote {args.out / name}")
        return 0

    stale: list[str] = []
    expected: dict[str, str] = {}
    for page in sorted({ab["page"] for ab in reg["ablations"]}):
        path = PAGES / f"{page}.md"
        text = path.read_text(encoding="utf-8")
        regions, plots = render_page(page, reg)
        expected.update(plots)
        updated = splice(text, regions, path)
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
