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
whenever at least one of its cells has a mean: one panel per dataset, each point
labelled with the value's short name (`short_values`), titled with the ablation's
`takeaway` and subtitled with the dataset, the sample, what varies and the model held
fixed. `plot` may list several metrics: a scatter then draws a row of panels per
metric. A metric with a `baseline` in the registry gets it as a dashed line, and an
ablation marked `families` colors and shapes its points by model family.

Each FAQ entry carries one chart. An ablation with a `by_size` key gets a chart of one
per-document score against the document's own size, drawn from the results file's
`per_doc` block, which is that chart: a mean over a dataset says which value wins, and
this says at what size it starts winning. Its scatter, like the chart of an ablation
with a `grid` (whose tables answer the question), goes to the details section.

`models` is optional: the model in force at each step of the pipeline. Each details
table states it in full, and a step whose model differs across the values reads
"varies (the setting under test)"; a chart names only the model held fixed.

`sample` is optional: the documents each run scored and the number of runs, which
every chart, caption and details table states ("20 × 3 runs", "N = 20 documents x 3
runs"). A document that failed was not scored, so the caption says how many were
left out.

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
    LOWER_IS_BETTER,
    SHARE_METRICS,
    draw_by_size,
    draw_plot,
    draw_scatter,
    fmt,
    lower_name,
    marginal_cost_of_error,
    money,
    x_axes_differ,
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
    f" Lines give the [marginal cost of error]({MCOE_PAGE}) from the cheapest value: "
    "the extra cost per error prevented."
)
BANDS_CAPTION = "Bands show ±2 standard errors."
DIFFER_CAPTION = " Cost axes differ by dataset."
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
                    f"{datasets[k].get('short', datasets[k]['name'])} — {steps[k][step]}"
                    for k in present
                    if step in steps[k]
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
        and ran(str(text))
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


def render_by_size(
    ab: dict[str, Any], reg: dict[str, Any], results: dict[str, Any]
) -> tuple[list[str], dict[str, str]]:
    """The by-size chart of an ablation that has one, and its markdown.

    One chart per pool of datasets (dataset_pools), drawn from the results file's
    per-document scores: where a mean over a dataset says which value wins, this says
    at what size it starts winning. An ablation with `strata` gets its table under
    each chart (render_strata).
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
        n_text = f"N = {len(drawn)} documents × {runs} runs"
        y_name = metrics[metric]["name"]
        strata = ab.get("strata") or {}
        things = str(strata.get("documents") or "documents")
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
        series = [(short_label(v, ab), pts) for v, pts in series]
        fixed = fixed_model(ab, models, used[0])
        subtitle = (
            f"{chart_name(pool[0], datasets)}, {len(drawn)} × {runs} runs · "
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
        )
        alt = (
            f"{title}: {lower_name(y_name)} per {thing} against "
            f"{lower_first(x_title.removesuffix(' (log scale)'))}"
        )
        caption = (
            f"*{y_name} per {thing}, against "
            f"{lower_first(x_title.removesuffix(' (log scale)'))} on a log scale: one "
            f"point per {thing} per {varies_text(ab)}, and a line through each one's "
            "running median."
            + (f" {LIST_SCORE_CAPTION}" if metric == "list_score" else "")
            + "*"
            + f" {n_text}. Datasets: "
            + ", ".join(dataset_label(k, datasets, short=True) for k in used)
            + "."
        )
        md += [f"![{alt}]({PLOTS_FROM_PAGES}/{name})", "", caption, ""]
        if ab.get("strata"):
            md += render_strata(ab, reg, results, sources)
    return md, plots


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

    def means(group: list[str], value: str, metric: str) -> float | None:
        found = [
            float(e[metric])
            for doc in group
            if metric
            in (e := (per_doc.get(f"{docs[doc][0]}|{value}") or {}).get(doc) or {})
        ]
        return statistics.fmean(found) if found else None

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
    groups = [
        [d for d, (_, x) in docs.items() if band_of(x, bands) == i]
        for i in range(len(bands))
    ]
    rows = list(zip(band_labels(bands, noun), groups, strict=True))
    rows.append((f"All {things}", sorted(docs)))
    for label, group in rows:
        if not group:
            continue
        scores = {v: means(group, v, score) for v in values}
        best = max((m for m in scores.values() if m is not None), default=None)
        row = [
            label if not label.startswith("All ") else f"**{label}**",
            f"{len(group):,}",
        ]
        for v in values:
            m = scores[v]
            text = EMPTY if m is None else f"{m:.2f}"
            if m is not None and best is not None and len(values) > 1:
                text = f"**{text}**" if f"{m:.2f}" == f"{best:.2f}" else text
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
    note = (
        f"*{things[:1].upper() + things[1:]} grouped by the {noun} their ground truth "
        f"holds. Each score and cost is a mean over the row's {things}; only {things} every "
        f"run of every value scored are counted, each once. The "
        f"{score_name} is Stickler's score for the list: matched rows over the larger "
        "of the true and returned row counts. Cost is each "
        f"{thing}'s own Amazon Bedrock and Amazon Textract metering"
        + (
            ", from each value's first, cold-cache run"
            if cold
            else ", averaged over runs that share no prompt cache"
        )
        + "; Agentic costs is agentic's cost over single-pass's for the same model. "
        f"The best {score_name} in each row is bold.* Datasets: "
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
    # One chart per question in the FAQ entry. Where a by-size chart or a grid of
    # tables answers the question, every other chart goes to the details.
    by_size_first = bool(results and ab.get("by_size") and results.get("per_doc"))
    charts_in_details = by_size_first or bool(results and ab.get("grid"))
    drawn_any = False
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
        scatter: list[tuple[str, list[Any], dict[str, tuple[float, str]]]] = []
        baseline_specs: dict[str, dict[str, Any]] = {}
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
            unit = per_unit(kind)
            # The first chart carries the takeaway. Under a by-size chart, which
            # carries it already, it carries its own (`details_takeaway`).
            first = not headline and not charts_in_details
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
                    x_money=x_metric == COST,
                    unit=unit,
                    mcoe=mcoe_metrics,
                    prices=prices,
                    families=bool(ab.get("families")),
                )
                out.plots[name] = svg
                x_words = (
                    f"cost per 1,000 {unit}"
                    if x_metric == COST
                    else lower_first(x_name)
                )
                alt = (
                    f"{title}: {lower_name(metric_name)} against {x_words}, one point "
                    f"per {varies_text(ab)}"
                )
                caption = (
                    f"*{metric_name} against {x_words}, one point per "
                    f"{varies_text(ab)}. {BANDS_CAPTION}"
                )
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
                    metric_name, panels, ab.get("runs", 0), prices=prices
                )
                alt = f"{metric_name} for each value of {ab['changes']}"
                caption = f"*{metric_name}. {CAPTION}*"
            plot_n = sample_sentence(sizes, plotted, datasets)
            if plot_n:
                caption += f" {plot_n}"
            caption += (
                " Datasets: "
                + ", ".join(dataset_label(k, datasets, short=True) for k in plotted)
                + "."
            )
            if first:
                caption += f" {details_link}."
            if on_cost:
                caption += f" {price_caveat(region)}"
            plot_md = [f"![{alt}]({PLOTS_FROM_PAGES}/{name})", "", caption, ""]
            if headline or charts_in_details:
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
