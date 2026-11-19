#!/usr/bin/env python3
"""Summarize Test Studio runs into a results file for the benchmark guidance pages.

Each value of an ablation is run as one or more Test Studio test runs. This script reads
those runs' results (the JSON `idp-cli test-result --output-dir` writes) and the
stack's tracking table, and writes benchmarks/results/guidance/<ID>.json, which
render_guidance.py turns into the page's tables and plots. The full per-document results
stay in Test Studio on the stack; the file keeps only the summary and the run IDs.

A run manifest names the runs:

    {
      "ablation": "EX-01",
      "stack": "sr-testing",
      "region": "us-east-1",
      "release": "0.6.15.dev1",
      "measured": "November 2026",
      "verdict": "...",
      "cache_isolated": ["RealKIE-FCC-Verified-20261113-173220", ...],
      "runs": {
        "realkie": {
          "Single-pass (Simple, default)": ["RealKIE-FCC-Verified-20261113-173220", ...],
          "Agentic (Advanced)": ["RealKIE-FCC-Verified-20261113-173219", ...]
        }
      }
    }

Metrics (cell keys `dataset|value|metric`, each a mean and standard error):

    weighted_accuracy   per document, averaged over its runs; SE across documents;
                        only documents every run of every value scored
    <step>_tokens       tokens per page for one step, per run; SE across runs
    total_tokens        tokens per page for every Bedrock step, per run; SE across runs
    median_seconds      median workflow time per document, per run; SE across runs
    all_fields_right    share of documents scoring 1.0, per run; SE across runs
    auroc               field-confidence AUROC, per run; SE across runs
    calibration_error   field-confidence ECE, per run; SE across runs
    errors_caught_30    share of wrong values found by reviewing the 30% of values
                        with the lowest confidence, per run; SE across runs
    errors_caught_30_random
                        the share the same review finds when it picks values at
                        random (about 0.30), per run; SE across runs
    ecarb_30            error capture at a 30% review budget: errors_caught_30 over
                        errors_caught_30_random (Stickler's `gain`), per run; SE
                        across runs
    page_accuracy       share of pages with the right class, per run; SE across runs
    section_accuracy    share of sections with the right pages and class, per run
    grouping_score      DocSplit packet score for grouping pages into documents
    classification_cost_per_1000_docs
                        USD per 1,000 documents for the classification steps alone
                        (CLASSIFICATION_STEPS): per run, the mean over the documents
                        it scored of each one's own metering for those steps, priced
                        like `cost` below; on the cost basis below, so the coldest
                        run alone (SE 0) unless the value's runs are cache-isolated

A metric is written only when the runs report it (confidence metrics need confidence
on; page accuracy needs page-level class ground truth).

The file also keeps each run's billed usage per document scored, so the renderer can
price it with config_library/pricing.yaml:

    "usage": {"<dataset key>|<value>": [
        {"bedrock/us.anthropic.claude-sonnet-5|inputTokens": 41250.5,
         "textract/analyze_document-Tables|pages": 2.1, ...},
        ...one entry per run
    ]}

`cost_basis` says which of those runs the renderer should price, per `dataset|value`:

    {"mode": "cold-run", "run": "<run ID>", "index": 0}   price this run alone
    {"mode": "cache-isolated"}                            price every run, mean and SE

Repeat runs of one value read the prompt cache the first run wrote, which a document
processed once in production would not, so by default only the coldest run (the one
that wrote the most cache; ties go to the earliest) carries the cost. A run started
under a per-run profile cannot share a cache with its siblings, and the manifest lists
it in `cache_isolated`; when every run of a value is isolated, all of them count.

`sample` is the sample size behind each `dataset|value`: the documents each run
scored, and how many runs there were, so the pages can state N without guessing. A
document that failed is not scored, so it is not in `docs`; `failed` says how many.

    "sample": {"realkie|Agentic (Advanced)": {
        "docs": [20, 20, 20], "failed": [0, 0, 0], "runs": 3
    }}

`per_doc` is each document's own scores, per `dataset|value`, averaged over the runs
that scored it, for the documents every run of every value scored (the rule weighted
accuracy uses). Only the keys a document has are written:

    "per_doc": {"realkie|Agentic (Advanced)": {"<document>": {
        "list_score": 0.62,          Stickler's score for the largest list field:
                                     matched rows over the larger of the true and
                                     returned row counts
        "list_rows": 247,            rows the ground truth has in that list
        "boundary_accuracy": 1.0,    from the document's own doc_split_metrics
        "label_accuracy": 1.0,
        "weighted_accuracy": 0.81,   Test Studio's weighted score for the document
        "cost": 0.0412               USD for this document: its own Bedrock and
                                     Textract metering, priced like the run costs
    }}}

`boundary_accuracy` and `label_accuracy` are also written as per-run cells, each the
mean over the documents that run scored, because Test Studio's run-level metrics do
not include them.

`sections_per_doc` and `pages_per_doc` are, per dataset, the mean number of true
sections and of pages in a scored document (each evaluation's `doc_split_metrics`
`total_splits` and `total_pages`), over every document some run of the dataset scored
(the common documents only, with `--common-docs`). Section accuracy is a share of
sections and page accuracy a share of pages, and a DocSplit packet holds several of
each, so the renderer needs these to count errors per packet:

    "sections_per_doc": {"docsplit_100": 4.12}, "pages_per_doc": {"docsplit_100": 11.3}

A document's `cost` comes from the `Metering` attribute of its tracking-table item,
priced with config_library/pricing.yaml by the renderer's own unit prices. It follows
`cost_basis`: the mean over every run when the value's runs are cache-isolated,
otherwise the coldest run alone. Tracking items are cached on disk per run (only
finished ones), so a re-collect reads nothing from DynamoDB.

`unfinished` names, per `dataset|value`, the documents some run of that value did not
score although another run of the dataset did: usually a document that outran Test
Studio's time limit. `status` is the document's tracking status in the runs that
missed it (COMPLETED means it finished after Test Studio stopped waiting):

    "unfinished": {"realkie_long|Agentic (Advanced)": {"<document>": {
        "runs": 3, "of": 3, "list_rows": 1490, "status": ["COMPLETED"]
    }}}

`models` says which model ran at each step, per `dataset|value`, so the pages can state
it on every chart:

    "models": {"realkie|Agentic (Advanced)": {
        "OCR": "Amazon Textract (Tables, Layout, Signatures)",
        "Classification": "Amazon Nova 2 Lite, page by page (no calls billed)",
        "Extraction": "Claude Sonnet 5, agentic",
        "Confidence": "Amazon Nova Lite, escalating to Claude Sonnet 5",
        "billed": {"Assessment": ["us.amazon.nova-lite-v1:0", ...], ...}
    }}

The four step lines come from the configuration each run used; `billed` is the Bedrock
models the cost breakdown actually charged for, which is how an escalation model that
the configuration does not name as a step's model still shows up.

Long-context pricing. pricing.yaml has one rate per unit, but Claude Haiku 5.5 bills a
whole request at long-context rates, five times the standard ones on every token unit,
when its prompt is more than 100,000 tokens (Bedrock model card, pricing). The prompt
is input tokens plus cache reads and cache writes: Anthropic's pricing page says a
request's prompt length "counts all of its input tokens, including cache reads and
cache writes", and a cache hit still sits in the context the request is billed for.
LONG_CONTEXT_RULES holds the rule per model ID (no region prefix): a threshold and a
multiplier. Metering is per document and step, not per request, so each step's
requests are sized from its own token totals and request count, laid out the way the
configuration makes the calls:

    a step whose prompt total is at most the threshold   no request can exceed it: none
    one request                                          its prompt is the total: exact
    holistic classification, packet-granularity          one call per window,
      splitting                                          windows(N) = 1 if N <= window
                                                         else ceil((N - overlap) /
                                                         (window - overlap)); the total
                                                         split by each window's pages.
                                                         More requests than windows: a
                                                         window was refused as too long
                                                         and retried as two smaller
                                                         ones, so the largest windows
                                                         are split the service's way
    single-pass extraction, confidence scoring,          one call per predicted section;
      section classification                             split by the section's pages
                                                         (its first k pages for
                                                         `pages: first_k`)
    any other request count (re-prompts, confidence      an even split (a re-prompt
      list batches), page-by-page classification         repeats the prompt; list
                                                         batches each resend the
                                                         section, and log to within 3%
                                                         of each other)
    agentic extraction, and the confidence step          not sized: a loop resending
      beside it                                          its growing conversation, and
                                                         shard confidence calls metered
                                                         with the confidence step's

The document's page count comes from its evaluation (else its sections, else its OCR
metering), the sections from the evaluation's `predicted_sections`; with --no-per-doc
there are no sections, and the per-section steps are split evenly.

A sized request over the threshold adds (multiplier - 1) times its standard price, its
share of the step's prompt tokens carrying that share of every unit. An unsized step
over the threshold stays at standard rates and is counted, with the most it could add
(every token at the long rate) beside it. Both the whole-document cost and the
classification cost include the surcharge, and the results file records it:

    "long_context": {"docsplit_100|Holistic · Claude Haiku 5.5": {
        "rules": {"anthropic.claude-haiku-5-5":
                  {"threshold": 100000, "multiplier": 5.0}},
        "documents": 298, "long_documents": 86,     document runs with a Haiku 5.5
                                                    request / with one at the long rate
        "requests": 3345, "long_requests": 105,     sized requests / at the long rate
        "unsized_documents": 0,                     document runs whose agentic step
                                                    passed the threshold, left standard
        "by_step": {"Classification": {...the same counts...}, ...},
        "surcharge": [0.0123, ...],                 USD per document added, per run,
                                                    in the order of `usage`
        "unsized_ceiling": [0.0, ...]               USD per document those could add
    }}

The counts are over every run of the value. The renderer adds `surcharge` to the cost
it prices from `usage`, so the whole pipeline's cost agrees with the per-document one.

Usage:
    AWS_PROFILE=... python3 benchmarks/harness/collect_guidance_results.py \\
        manifest.json --results-dir /tmp/ex01/results [--out scratch/EX-01.json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3

# lib needs idp_common; put this checkout's on the path, ahead of any other.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib" / "idp_common_pkg"))
from render_guidance import load_registry, unit_price  # noqa: E402

from lib import METERING_ATTRIBUTE, metering_of_item  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "benchmarks" / "results" / "guidance"
#: Test Studio cost-breakdown step names, by the registry's metric key. Detecting
#: sections first meters its two calls as Splitting and SectionClassification.
STEP_METRICS = {
    "ocr_tokens": ("OCR",),
    "classification_tokens": ("Classification", "Splitting", "SectionClassification"),
    "extraction_tokens": ("Extraction",),
    "scoring_tokens": ("Assessment",),
}
#: Split metrics a document's own evaluation reports, which the run aggregate does
#: not break down per document.
DOC_SPLIT_METRICS = ("boundary_accuracy", "label_accuracy")
#: Where per-document evaluations are cached: one file per run and document, plus one
#: row-count file per test set under `_rows/`.
# Caches live beside the runner's workdir, outside /tmp, which a reboot empties.
WORKDIR = Path(
    os.environ.get("GUIDANCE_WORKDIR", "~/bench-work/guidance-runs")
).expanduser()
DOC_EVAL_CACHE = WORKDIR / "doceval"
DOC_EVAL_WORKERS = 8
#: Where tracking-table items are cached: one file per run, the fields below only.
TRACKING_CACHE = WORKDIR / "tracking"
TRACKING_FIELDS = (
    "WorkflowStartTime",
    "CompletionTime",
    "ObjectStatus",
    METERING_ATTRIBUTE,
)
#: The review budget behind `errors_caught_30`: the share of values a reviewer checks,
#: lowest confidence first, as Test Studio keys it under error_capture_at_budget.
REVIEW_BUDGET = "0.30"
#: Cost-breakdown services kept as usage, and units that carry no price of their own.
PRICED_SERVICES = ("bedrock/", "textract/", "bda/", "decision_model/")
UNPRICED_UNITS = ("requests", "totalTokens")
#: Display names by model ID, without a region prefix or a long-context suffix. A
#: model that is not listed keeps its raw ID: a wrong name is worse than an ugly one.
MODEL_NAMES = {
    "amazon.nova-lite-v1:0": "Amazon Nova Lite",
    "amazon.nova-pro-v1:0": "Amazon Nova Pro",
    "amazon.nova-2-lite-v1:0": "Amazon Nova 2 Lite",
    "amazon.nova-premier-v1:0": "Amazon Nova Premier",
    "anthropic.claude-haiku-5-5": "Claude Haiku 5.5",
    "anthropic.claude-sonnet-4-6": "Claude Sonnet 4.6",
    "anthropic.claude-sonnet-5": "Claude Sonnet 5",
    "anthropic.claude-sonnet-5-5": "Claude Sonnet 5.5",
    "anthropic.claude-opus-5": "Claude Opus 5",
    "anthropic.claude-opus-5-5": "Claude Opus 5.5",
    "openai.gpt-6-astra": "OpenAI GPT-6 Astra",
    "xai.grok-4.6": "xAI Grok 4.6",
}
REGION_PREFIXES = ("us-gov.", "us.", "eu.", "apac.", "ca.", "global.")
FEATURE_NAMES = {
    "TABLES": "Tables",
    "LAYOUT": "Layout",
    "SIGNATURES": "Signatures",
    "FORMS": "Forms",
    "QUERIES": "Queries",
}
GRANULARITY = {"page": "page by page", "packet": "holistic"}
EXTRACTION_MODES = {"simple": "single-pass", "advanced": "agentic"}
#: The steps of a pipeline, in order, and the cost-breakdown steps that bill each one.
STEP_BILLING = {
    "OCR": ("OCR",),
    "Classification": ("Classification", "Splitting", "SectionClassification"),
    "Extraction": ("Extraction",),
    "Confidence": ("Assessment",),
}
#: The metering steps `classification_cost_per_1000_docs` counts. Classifying in one
#: step meters `Classification/bedrock/<model>`; splitting first meters
#: `Splitting/bedrock/<model>` and then `SectionClassification/bedrock/<model>`, or
#: `SectionClassification/decision_model/<function>` (the decision model's Lambda
#: duration) when a decision model names the sections.
CLASSIFICATION_STEPS = STEP_BILLING["Classification"]
CLASSIFICATION_COST = "classification_cost_per_1000_docs"


@dataclass(frozen=True)
class LongContextRule:
    """A request whose prompt is more than `threshold` tokens is billed at `multiplier`
    times the standard rate on every token unit (input, output, cache read and write)."""

    threshold: int
    multiplier: float


#: Long-context pricing by model ID without a region prefix (see the module docstring).
#: Claude Haiku 5.5: Bedrock model card and price list, 5x above 100,000 prompt tokens.
LONG_CONTEXT_RULES: dict[str, LongContextRule] = {
    "anthropic.claude-haiku-5-5": LongContextRule(threshold=100_000, multiplier=5.0),
}
#: The metering units that make up a request's prompt, and every billed token unit.
PROMPT_UNITS = ("inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
TOKEN_UNITS = (*PROMPT_UNITS, "outputTokens")
#: The classification service's packet window when a configuration leaves it unset.
WINDOW_PAGES = 20
OVERLAP_PAGES = 2


def mean_se(values: list[float]) -> dict[str, float]:
    m = statistics.fmean(values)
    se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return {"mean": round(m, 6), "se": round(se, 6)}


def base_model_id(model_id: Any) -> str:
    """A model ID without its region prefix or long-context suffix."""
    base = str(model_id).removesuffix(":1m")
    for prefix in REGION_PREFIXES:
        if base.startswith(prefix):
            return base[len(prefix) :]
    return base


def model_name(model_id: Any) -> str:
    """The display name of a model ID, or the raw ID when we have no name for it."""
    if not model_id:
        return ""
    return MODEL_NAMES.get(base_model_id(model_id), str(model_id))


def ocr_line(cfg: dict[str, Any]) -> str:
    """What read the pages: Textract with its features, or a vision model."""
    ocr = cfg.get("ocr") or {}
    backend = str(ocr.get("backend") or "")
    if backend == "textract":
        names = [str(f.get("name") or "") for f in (ocr.get("features") or [])]
        pretty = ", ".join(FEATURE_NAMES.get(n, n.title()) for n in names if n)
        return (
            f"Amazon Textract ({pretty})" if pretty else "Amazon Textract (text only)"
        )
    if backend == "bedrock":
        return f"{model_name(ocr.get('model_id'))}, reading the page images"
    if backend == "none":
        return "No OCR step"
    return backend or "—"


def classification_line(cfg: dict[str, Any]) -> str:
    """What classified the pages, and at which granularity."""
    cls = cfg.get("classification") or {}
    if str(cls.get("strategy") or "") == "split_then_classify":
        stc = cls.get("split_then_classify") or {}
        splitting = (stc.get("splitting") or {}).get("llm") or {}
        section = (stc.get("section_classification") or {}).get("llm") or {}
        how = GRANULARITY.get(str(splitting.get("granularity") or ""), "")
        found = model_name(splitting.get("model"))
        head = (
            f"{found} finds the sections ({how})"
            if how
            else f"{found} finds the sections"
        )
        return f"{head}, then {model_name(section.get('model'))} classifies them"
    combined = cls.get("combined") or {}
    model = model_name((combined.get("llm") or {}).get("model"))
    how = GRANULARITY.get(str(combined.get("granularity") or ""), "")
    return f"{model}, {how}" if how else model


def extraction_line(cfg: dict[str, Any]) -> str:
    """What extracted the fields, in which mode, and what any failures escalate to."""
    ex = cfg.get("extraction") or {}
    mode = str(ex.get("mode") or "")
    text = model_name(ex.get("model"))
    if mode:
        text += f", {EXTRACTION_MODES.get(mode, mode)}"
    validation = ex.get("validation") or {}
    escalation = model_name(validation.get("escalation_model"))
    if (
        str(validation.get("fail_action") or "") == "escalate"
        and escalation
        and escalation not in text
    ):
        text += f", failed fields to {escalation}"
    return text


def confidence_line(cfg: dict[str, Any]) -> str:
    """What scored the confidence of each value, and what it escalates to."""
    ex = cfg.get("extraction") or {}
    conf = ex.get("confidence") or {}
    mode = str(conf.get("mode") or "off")
    if mode == "off":
        return "Off"
    if mode == "integrated":
        text = f"{model_name(ex.get('model'))}, integrated into extraction"
    else:
        text = model_name(conf.get("model"))
    escalation = model_name(conf.get("escalation_model"))
    if conf.get("escalation_enabled") and escalation and escalation not in text:
        text += f", escalating to {escalation}"
    return text


def config_models(cfg: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    """The model in force at each step, and which of those steps call Bedrock."""
    lines = {
        "OCR": ocr_line(cfg),
        "Classification": classification_line(cfg),
        "Extraction": extraction_line(cfg),
        "Confidence": confidence_line(cfg),
    }
    bedrock = {"Classification", "Extraction"}
    if str(((cfg.get("ocr") or {}).get("backend")) or "") == "bedrock":
        bedrock.add("OCR")
    if lines["Confidence"] != "Off":
        bedrock.add("Confidence")
    return lines, bedrock


def billed_models(result: dict[str, Any]) -> dict[str, list[str]]:
    """The Bedrock models the cost breakdown charged for, by step."""
    out: dict[str, list[str]] = {}
    steps = {s for names in STEP_BILLING.values() for s in names}
    for step, items in (result.get("costBreakdown") or {}).items():
        if step not in steps:
            continue
        found = set()
        for key, item in items.items():
            if key.startswith("bedrock/") and float(item.get("value") or 0):
                service_id, _, _ = key.rpartition("_")
                found.add(service_id.removeprefix("bedrock/"))
        if found:
            out[step] = sorted(found)
    return out


def with_billed(
    lines: dict[str, str], bedrock: set[str], billed: dict[str, list[str]]
) -> None:
    """Note any model the runs were billed for that the step's own line leaves out."""
    for step, names in STEP_BILLING.items():
        if step not in lines:
            continue
        ran = sorted({m for name in names for m in billed.get(name, [])})
        extra = [
            n for n in dict.fromkeys(model_name(m) for m in ran) if n not in lines[step]
        ]
        if extra:
            lines[step] += ", plus " + ", ".join(extra)
        elif not ran and step in bedrock:
            lines[step] += " (no calls billed)"


def step_tokens(result: dict[str, Any], steps: tuple[str, ...] | None) -> int:
    """Bedrock tokens (input, output, cache reads and writes) for some steps, or all."""
    total = 0
    for name, items in result.get("costBreakdown", {}).items():
        if steps and name not in steps:
            continue
        for key, item in items.items():
            if key.startswith("bedrock/") and key.endswith("_totalTokens"):
                total += int(item["value"])
    return total


def cache_writes(result: dict[str, Any]) -> float:
    """Prompt-cache tokens this run wrote, over every step: how cold its cache was."""
    total = 0.0
    for items in (result.get("costBreakdown") or {}).values():
        for key, item in items.items():
            if key.startswith("bedrock/") and key.endswith("_cacheWriteInputTokens"):
                total += float(item.get("value") or 0)
    return total


def scored_docs(result: dict[str, Any]) -> int:
    """How many documents this run scored: a failed document is not one of them."""
    return int(result.get("completedFiles") or 0) or (
        int(result.get("filesCount") or 0) - int(result.get("failedFiles") or 0)
    )


def run_usage(result: dict[str, Any]) -> dict[str, float]:
    """Bedrock and Textract usage per document scored, summed over every step.

    Cost-breakdown keys look like `bedrock/<model id>_<unit>`; the result's keys look
    like `bedrock/<model id>|<unit>`.
    """
    docs = scored_docs(result)
    if docs <= 0:
        return {}
    totals: dict[str, float] = {}
    for items in (result.get("costBreakdown") or {}).values():
        for key, item in items.items():
            if not key.startswith(PRICED_SERVICES):
                continue
            # The item names its unit, and a unit can hold an underscore
            # (`gb_seconds`), so split the key at the unit rather than at the last "_".
            unit = str(item.get("unit") or "")
            service_id = (
                key[: -len(unit) - 1]
                if unit and key.endswith(f"_{unit}")
                else key.rpartition("_")[0]
            )
            unit = unit or key.rpartition("_")[2]
            if not service_id or unit in UNPRICED_UNITS:
                continue
            name = f"{service_id}|{unit}"
            totals[name] = totals.get(name, 0.0) + float(item.get("value") or 0)
    return {k: round(v / docs, 4) for k, v in sorted(totals.items())}


def metering_cost(
    metering: dict[str, Any], unpriced: set[str], steps: tuple[str, ...] = ()
) -> tuple[float, float]:
    """One document's Bedrock and Textract cost in USD, from its own metering, and
    the part of it the metering `steps` billed.

    Metering keys look like `<step>/bedrock/<model id>` with a unit map under each,
    and are priced the way the renderer prices a run's usage: per `bedrock/<model
    id>|<unit>`, with `requests` and `totalTokens` left out. Usage with no price
    counts as $0 and is added to `unpriced`.
    """
    total = 0.0
    in_steps = 0.0
    for key, units in metering.items():
        step, _, service_id = str(key).partition("/")
        if not service_id.startswith(PRICED_SERVICES) or not isinstance(units, dict):
            continue
        for unit, amount in units.items():
            if unit in UNPRICED_UNITS or not amount:
                continue
            price = unit_price(service_id, unit)
            if price is None:
                unpriced.add(f"{service_id}|{unit}")
                continue
            total += float(amount) * price
            if step in steps:
                in_steps += float(amount) * price
    return total, in_steps


@dataclass(frozen=True)
class Layout:
    """How a step lays out one document's requests: `kind` names the layout and
    `pages` is the pages each request carries, None when that is not known. `split`
    is the overlap a packet window refused as too large repeats when the
    classification service retries it as two smaller windows; None where windows
    are never split (`window_pages: 0`, or not a packet call)."""

    kind: str
    pages: tuple[int, ...] | None = None
    split: int | None = None


@dataclass(frozen=True)
class LongContextStep:
    """One document step's requests on a model with long-context pricing.

    `method` says how the requests were sized: `under` (the step's whole prompt is
    within the threshold), `exact` (one request), `windows`, `sections` or `pages` (the
    total split by the pages each request carries), `even` (split evenly), or
    `unsized` (an agentic loop, left at standard rates). `requests` counts the sized
    requests only, and `ceiling` is what an unsized step could add at most. `model`
    is the model ID without a region prefix, as LONG_CONTEXT_RULES keys it.
    """

    step: str
    model: str
    requests: int
    long_requests: int
    method: str
    surcharge: float
    ceiling: float = 0.0


def packet_windows(
    n_pages: int, window: int = WINDOW_PAGES, overlap: int = OVERLAP_PAGES
) -> list[int]:
    """Pages in each window of a packet call, as the classification service lays
    them out (`_packet_windows`): the whole packet when it fits one window (or the
    window is 0, never split), else windows of `window` pages, each starting
    `window - overlap` pages after the one before.

    For more than `window` pages that is ceil((N - overlap) / (window - overlap))
    windows.
    """
    if n_pages <= 0:
        return []
    if window <= 0 or n_pages <= window:
        return [n_pages]
    step = max(1, window - overlap)
    out: list[int] = []
    start = 0
    while start < n_pages:
        out.append(min(window, n_pages - start))
        if start + window >= n_pages:
            break
        start += step
    return out


def split_window(n_pages: int, overlap: int) -> list[int]:
    """The two windows the classification service retries a window of `n_pages`
    refused as too large as (`_split_overflowing_window`): the first half, and the
    rest with up to `overlap` of the first half's pages repeated, capped so both are
    smaller than the window."""
    if n_pages < 2:
        return [n_pages]
    mid = n_pages // 2
    cap = max(1, (mid - 1) // 2) if mid >= 2 else 0
    return [mid, n_pages - mid + max(0, min(overlap, cap))]


def classifying_layout(llm: dict[str, Any], n_pages: int) -> Layout:
    """The requests of a classifying call configured by `llm` (a `combined` or a
    `splitting.llm` block): one per window at packet granularity, else one per page."""
    if not n_pages:
        return Layout("even")
    if str(llm.get("granularity") or "") == "packet":
        packet = llm.get("packet") or {}
        window = packet.get("window_pages")
        overlap = packet.get("overlap_pages")
        window = WINDOW_PAGES if window is None else int(window)
        overlap = OVERLAP_PAGES if overlap is None else int(overlap)
        return Layout(
            "windows",
            tuple(packet_windows(n_pages, window, overlap)),
            overlap if window > 0 else None,
        )
    return Layout("pages", (1,) * n_pages)


def request_layout(
    step: str, cfg: dict[str, Any], n_pages: int, sections: list[int] | None
) -> Layout:
    """How the configuration lays out one document's requests for a metering step.

    `n_pages` is the document's page count and `sections` the pages in each section
    the pipeline found (None when not known). Single-pass extraction and confidence
    scoring make one call per section, as section classification does, which reads
    only a section's first k pages under `pages: first_k`; a section's list batches
    each resend its pages. Agentic extraction is a loop (`loop`), whose requests
    resend a growing conversation, and its shards' confidence calls are metered with
    the confidence step's (`mixed`, validated against the Lambda logs: an EX-07
    document metered 10 calls, 8 of them about 106,000 tokens in the confidence
    Lambda and 2 small ones in the shard runtime).
    """
    cls = cfg.get("classification") or {}
    stc = cls.get("split_then_classify") or {}
    found = tuple(sections) if sections is not None else None
    if step == "Classification":
        return classifying_layout(cls.get("combined") or {}, n_pages)
    if step == "Splitting":
        return classifying_layout(
            (stc.get("splitting") or {}).get("llm") or {}, n_pages
        )
    if step == "SectionClassification":
        sc = stc.get("section_classification") or {}
        if found is not None and str(sc.get("pages") or "") == "first_k":
            k = int(sc.get("k") or 1)
            return Layout("sections", tuple(min(n, k) for n in found))
        return Layout("sections", found)
    ex = cfg.get("extraction") or {}
    agentic = (
        bool((ex.get("agentic") or {}).get("enabled")) or ex.get("mode") == "advanced"
    )
    if step == "Extraction":
        return Layout("loop") if agentic else Layout("sections", found)
    if step == "Assessment":
        # Agentic extraction's shards meter their own confidence calls under the same
        # step, so the count no longer describes the per-section calls.
        return Layout("mixed") if agentic else Layout("sections", found)
    return Layout("even")


def request_prompts(
    prompt: float, requests: int, layout: Layout
) -> tuple[list[float] | None, str]:
    """Each request's prompt tokens, estimated from the step's total, and the method.

    One request is the total. A layout whose request count matches the metering splits
    the total by the pages each request carries. Packet windows with more requests
    than windows had windows refused as too large ("Input is too long for requested
    model") and retried as two smaller ones; the largest windows are split until the
    count matches. Any other count is split evenly (a re-prompt repeats the whole
    prompt; list batches of confidence scoring each resend the section, so they are
    near equal). An agentic loop, the confidence step beside one, or a step with no
    request count, is not sized.
    """
    if requests == 1:
        return [prompt], "exact"
    if layout.kind in ("loop", "mixed") or requests <= 0:
        return None, "unsized"
    pages = list(layout.pages or ())
    if layout.split is not None:
        while pages and len(pages) < requests:
            i = max(range(len(pages)), key=lambda j: pages[j])
            if pages[i] < 2:
                break
            pages[i : i + 1] = split_window(pages[i], layout.split)
    if pages and len(pages) == requests and sum(pages) > 0:
        total = sum(pages)
        return [prompt * n / total for n in pages], layout.kind
    return [prompt / requests] * requests, "even"


def long_context_steps(
    metering: dict[str, Any],
    layout: Callable[[str], Layout],
    rules: dict[str, LongContextRule] = LONG_CONTEXT_RULES,
) -> list[LongContextStep]:
    """The long-context pricing of one document's metering, per step and model.

    Only models with a rule in `rules` are returned. A step whose prompt total is
    within the threshold has no request over it. Otherwise its requests are sized
    (request_prompts), and each one over the threshold adds `multiplier - 1` times its
    standard price: its share of the step's prompt tokens, applied to every token unit
    the step was billed for.
    """
    out: list[LongContextStep] = []
    for key, units in metering.items():
        step, _, service_id = str(key).partition("/")
        if not service_id.startswith("bedrock/") or not isinstance(units, dict):
            continue
        model = base_model_id(service_id.removeprefix("bedrock/"))
        rule = rules.get(model)
        if rule is None:
            continue
        prompt = sum(float(units.get(u) or 0) for u in PROMPT_UNITS)
        requests = int(units.get("requests") or 0)
        standard = 0.0
        for unit in TOKEN_UNITS:
            price = unit_price(service_id, unit)
            if price is not None:
                standard += float(units.get(unit) or 0) * price
        extra = rule.multiplier - 1
        if prompt <= rule.threshold:
            out.append(LongContextStep(step, model, max(requests, 0), 0, "under", 0.0))
            continue
        prompts, method = request_prompts(prompt, requests, layout(step))
        if prompts is None:
            out.append(
                LongContextStep(step, model, 0, 0, method, 0.0, extra * standard)
            )
            continue
        long = [p for p in prompts if p > rule.threshold]
        out.append(
            LongContextStep(
                step,
                model,
                len(prompts),
                len(long),
                method,
                extra * standard * sum(long) / prompt,
            )
        )
    return out


def section_pages(evaluation: dict[str, Any] | None) -> list[int] | None:
    """The pages in each section the pipeline found, from the document's evaluation."""
    split = (evaluation or {}).get("doc_split_metrics") or {}
    found = split.get("predicted_sections")
    if not found:
        return None
    return [len(s.get("page_indices") or []) for s in found]


def document_pages(
    evaluation: dict[str, Any] | None,
    sections: list[int] | None,
    metering: dict[str, Any],
) -> int:
    """A document's page count: its evaluation's, else its sections' pages, else the
    pages its OCR step was billed for. The last can fall short of the document (a
    40-page DocSplit packet metered 13 Textract pages), so it comes last; a wrong count
    only lays the windows out wrong, and a layout whose request count does not match
    the metering is split evenly instead."""
    split = (evaluation or {}).get("doc_split_metrics") or {}
    if split.get("total_pages"):
        return int(split["total_pages"])
    if sections:
        return sum(sections)
    return max(
        (
            int(units.get("pages") or 0)
            for key, units in metering.items()
            if str(key).startswith("OCR/") and isinstance(units, dict)
        ),
        default=0,
    )


def registry_config(ablation: str, value: str) -> dict[str, Any]:
    """A configuration for a run whose result carries none: the registry's settings
    for the value (its baseline's, then its own `set`), as nested dicts. Only what the
    ablation sets is there; packet windows not set default to the service's own."""
    reg = load_registry()
    ab = next((a for a in reg.get("ablations") or [] if a.get("id") == ablation), {})
    dotted: dict[str, Any] = {}
    if ab.get("baseline"):
        dotted.update(
            ((reg.get("baselines") or {}).get(ab["baseline"]) or {}).get("set") or {}
        )
    values = ab.get("values") or []
    sets = ab.get("set") or []
    if value in values and values.index(value) < len(sets):
        dotted.update(sets[values.index(value)] or {})
    cfg: dict[str, Any] = {}
    for path, v in dotted.items():
        node = cfg
        *parents, leaf = str(path).split(".")
        for name in parents:
            node = node.setdefault(name, {})
        node[leaf] = v
    return cfg


class LongContextTally:
    """Counts of long-context requests over a value's runs, in total and by step."""

    FIELDS = (
        "documents",
        "long_documents",
        "requests",
        "long_requests",
        "unsized_documents",
    )

    def __init__(self) -> None:
        self.total = dict.fromkeys(self.FIELDS, 0)
        self.by_step: dict[str, dict[str, int]] = {}
        self.models: set[str] = set()

    def add(self, steps: list[LongContextStep]) -> None:
        """Count one document's steps (one run of it)."""
        if not steps:
            return
        self.models |= {s.model for s in steps}
        self._count(self.total, steps)
        for name in dict.fromkeys(s.step for s in steps):
            counts = self.by_step.setdefault(name, dict.fromkeys(self.FIELDS, 0))
            self._count(counts, [s for s in steps if s.step == name])

    @staticmethod
    def _count(counts: dict[str, int], steps: list[LongContextStep]) -> None:
        counts["documents"] += 1
        counts["long_documents"] += any(s.long_requests for s in steps)
        counts["requests"] += sum(s.requests for s in steps)
        counts["long_requests"] += sum(s.long_requests for s in steps)
        counts["unsized_documents"] += any(s.method == "unsized" for s in steps)


def list_field(result: dict[str, Any]) -> str | None:
    """The run's largest list field: the parent whose cells were scored most often.

    Test Studio scores a list cell by cell under `fieldMetrics["<List>.<Field>"]`, so
    a top-level field with leaf fields under it is a list.
    """
    fields = result.get("fieldMetrics") or {}
    parents: dict[str, int] = {}
    for name, m in fields.items():
        if "." in name:
            parent = name.split(".", 1)[0]
            parents[parent] = parents.get(parent, 0) + int(
                m.get("accuracy_observations") or 0
            )
    return max(parents, key=lambda p: parents[p]) if parents else None


def list_cells(result: dict[str, Any]) -> tuple[int, int, int] | None:
    """Cell counts (tp, fn, fp) over the leaf fields of the largest list field.

    The weighted score counts a whole list as one field, so it barely moves when
    rows go missing; these counts are what show a lost row.
    """
    fields = result.get("fieldMetrics") or {}
    parent = list_field(result)
    if parent is None:
        return None
    tp = fn = fp = 0
    for name, m in fields.items():
        if name.startswith(parent + "."):
            tp += int(m.get("tp") or 0)
            fn += int(m.get("fn") or 0)
            fp += int(m.get("fp") or 0)
    return tp, fn, fp


def run_metrics(result: dict[str, Any]) -> dict[str, float]:
    """Per-run metrics that Test Studio reports directly, when present."""
    out: dict[str, float] = {}
    cells = list_cells(result)
    if cells:
        tp, fn, fp = cells
        if tp + fn:
            out["list_recall"] = tp / (tp + fn)
        if tp + fp:
            out["list_precision"] = tp / (tp + fp)
    scores = [float(v) for v in result.get("weightedOverallScores", {}).values()]
    if scores:
        out["all_fields_right"] = sum(v >= 0.9999 for v in scores) / len(scores)
    overall = (result.get("confidenceMetrics") or {}).get("overall") or {}
    if (overall.get("auroc") or {}).get("value") is not None:
        out["auroc"] = float(overall["auroc"]["value"])
    if (overall.get("ece") or {}).get("value") is not None:
        out["calibration_error"] = float(overall["ece"]["value"])
    budget = (
        ((overall.get("error_capture_at_budget") or {}).get("budgets") or {}).get(
            REVIEW_BUDGET
        )
    ) or {}
    if budget.get("pct_errors_caught") is not None:
        out["errors_caught_30"] = float(budget["pct_errors_caught"])
    if budget.get("pct_errors_random") is not None:
        out["errors_caught_30_random"] = float(budget["pct_errors_random"])
    # ECARB: errors the review catches over what a random review of as many values
    # catches. Stickler reports it as `gain`; an older result has only the shares.
    caught, by_chance = budget.get("pct_errors_caught"), budget.get("pct_errors_random")
    if budget.get("gain") is not None:
        out["ecarb_30"] = float(budget["gain"])
    elif caught is not None and by_chance:
        out["ecarb_30"] = float(caught) / float(by_chance)
    split = result.get("splitClassificationMetrics") or {}
    if split.get("page_level_accuracy") is not None:
        out["page_accuracy"] = float(split["page_level_accuracy"])
    if split.get("split_accuracy_without_order") is not None:
        out["section_accuracy"] = float(split["split_accuracy_without_order"])
    graded = ((result.get("gradedPacketMetrics") or {}).get("mean")) or {}
    if graded.get("final_score") is not None:
        out["grouping_score"] = float(graded["final_score"])
    return out


def doc_list_score(doc: dict[str, Any], field: str) -> float | None:
    """Stickler's score for one list field of a document: matched rows over the larger
    of the true and returned row counts.

    The score belongs to a section, so it stands for the document only when the
    document is one section. A packet's first section is not the document, and its
    list score would read as one.
    """
    sections = doc.get("section_results") or []
    if len(sections) != 1:
        return None
    scores = (
        (sections[0].get("stickler_comparison_result") or {}).get("field_scores")
    ) or {}
    value = scores.get(field)
    return None if value is None else float(value)


def doc_split_scores(doc: dict[str, Any]) -> dict[str, float]:
    """The split metrics a document's own evaluation reports, when it has them."""
    split = doc.get("doc_split_metrics") or {}
    return {
        metric: float(split[metric])
        for metric in DOC_SPLIT_METRICS
        if split.get(metric) is not None
    }


class DocEvals:
    """Per-document evaluations, and the ground-truth length of a document's list.

    Test Studio writes one evaluation per document to the output bucket at
    `<run ID>/<document>/evaluation/results.json`, and a test set's ground truth to
    the test set bucket at `<test set>/baseline/<document>/sections/1/result.json`.
    Both are fetched once and cached on disk: a re-collect of the same runs reads
    nothing from S3. Row counts are cached one small file per test set, so the
    baselines themselves are never kept.
    """

    def __init__(self, resources: dict[str, str], region: str, cache: Path) -> None:
        self.s3 = boto3.client("s3", region_name=region)
        self.output_bucket = resources.get("OutputBucket")
        self.testset_bucket = resources.get("TestSetBucket")
        self.cache = cache
        self.rows_cache = cache / "_rows"
        self.missing: set[str] = set()

    def _get(self, bucket: str, key: str) -> dict[str, Any] | None:
        try:
            body = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:  # a document with no evaluation is not an error
            self.missing.add(f"s3://{bucket}/{key}: {type(exc).__name__}")
            return None
        return json.loads(body)

    def _cached(self, bucket: str, key: str, path: Path) -> dict[str, Any] | None:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        doc = self._get(bucket, key)
        if doc is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
        return doc

    def evaluations(self, run_id: str, docs: list[str]) -> dict[str, dict[str, Any]]:
        """Every document's evaluation for one run, by document name."""
        if not self.output_bucket:
            return {}
        out: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=DOC_EVAL_WORKERS) as pool:
            fetched = pool.map(
                lambda doc: (
                    doc,
                    self._cached(
                        self.output_bucket or "",
                        f"{run_id}/{doc}/evaluation/results.json",
                        self.cache / run_id / doc / "evaluation" / "results.json",
                    ),
                ),
                docs,
            )
            for doc, result in fetched:
                if result is not None:
                    out[doc] = result
        return out

    def rows(self, testset: str, docs: list[str], field: str) -> dict[str, int]:
        """How many rows the ground truth has in `field`, per document."""
        if not self.testset_bucket:
            return {}
        path = self.rows_cache / f"{testset}.json"
        known: dict[str, dict[str, int]] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )
        wanted = [d for d in docs if field not in known.get(d, {})]
        with ThreadPoolExecutor(max_workers=DOC_EVAL_WORKERS) as pool:
            fetched = pool.map(
                lambda doc: (
                    doc,
                    self._get(
                        self.testset_bucket or "",
                        f"{testset}/baseline/{doc}/sections/1/result.json",
                    ),
                ),
                wanted,
            )
            for doc, baseline in fetched:
                if baseline is None:
                    continue
                truth = baseline.get("inference_result") or {}
                known.setdefault(doc, {}).update(
                    {k: len(v) for k, v in truth.items() if isinstance(v, list)}
                )
        if wanted:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(known, indent=0, sort_keys=True), "utf-8")
        return {d: known[d][field] for d in docs if field in known.get(d, {})}


def pages(result: dict[str, Any]) -> int:
    """The run's page count: from the split metrics, else from Textract's billed pages.

    Test Studio sometimes leaves `splitClassificationMetrics` empty; a run without
    either count returns 0, and its tokens are not counted per page at all, rather
    than divided by 1.
    """
    split = int(
        (result.get("splitClassificationMetrics") or {}).get("total_pages") or 0
    )
    if split:
        return split
    ocr = ((result.get("costBreakdown") or {}).get("OCR")) or {}
    billed = [
        int(item.get("value") or 0)
        for key, item in ocr.items()
        if key.startswith(("textract/", "bda/"))
        and isinstance(item, dict)
        and item.get("unit") == "pages"
    ]
    return max(billed, default=0)


class TrackingItems:
    """Each document's tracking-table item, as far as the collector reads it.

    An item is `PK = doc#<run ID>/<document>`, `SK = none`. Only TRACKING_FIELDS are
    read, with the metering attribute decoded by lib.metering_of_item, and only finished items (those
    with a CompletionTime) are cached, one file per run, so a document still running
    is read again next time.
    """

    def __init__(self, table_name: str, region: str, cache: Path) -> None:
        self.ddb = boto3.client("dynamodb", region_name=region)
        self.table_name = table_name
        self.cache = cache

    def _get(self, run_id: str, doc: str) -> dict[str, Any] | None:
        names = {f"#f{i}": f for i, f in enumerate(TRACKING_FIELDS)}
        pk = f"doc#{run_id}/{doc}"
        raw = self.ddb.get_item(
            TableName=self.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": "none"}},
            ProjectionExpression=", ".join(names),
            ExpressionAttributeNames=names,
        ).get("Item")
        if not raw:
            return None
        item: dict[str, Any] = {
            k: v["S"]
            for k, v in raw.items()
            if k in TRACKING_FIELDS and k != METERING_ATTRIBUTE and "S" in v
        }
        metering = metering_of_item(raw, where=pk)
        if metering.is_present:
            item[METERING_ATTRIBUTE] = metering.value
        else:
            # Left out of the costs, never priced as $0.00.
            print(f"warning: {metering.error}", file=sys.stderr)
        return item

    def items(self, run_id: str, docs: list[str]) -> dict[str, dict[str, Any]]:
        """The items of one run's documents, by document name."""
        path = self.cache / f"{run_id}.json"
        known: dict[str, dict[str, Any]] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )
        wanted = [d for d in dict.fromkeys(docs) if d not in known]
        found = dict(known)
        fresh = False
        with ThreadPoolExecutor(max_workers=DOC_EVAL_WORKERS) as pool:
            for doc, item in pool.map(lambda d: (d, self._get(run_id, d)), wanted):
                if not item:
                    continue
                found[doc] = item
                if item.get("CompletionTime"):
                    known[doc] = item
                    fresh = True
        if fresh:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(known, sort_keys=True), encoding="utf-8")
        return {d: found[d] for d in docs if d in found}


def doc_cost_over_runs(
    by_run: dict[int, float], all_isolated: bool, cold_index: int
) -> float | None:
    """A document's cost over a value's runs, on the basis the run costs use.

    Cache-isolated runs are priced alike, so the cost is the mean over the runs that
    metered the document; otherwise only the coldest run counts, because the others
    read the prompt cache it wrote.
    """
    if all_isolated:
        return statistics.fmean(by_run.values()) if by_run else None
    return by_run.get(cold_index)


def workflow_seconds(items: dict[str, dict[str, Any]]) -> list[float]:
    out = []
    for item in items.values():
        if not item.get("CompletionTime") or not item.get("WorkflowStartTime"):
            continue
        start = datetime.fromisoformat(item["WorkflowStartTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(item["CompletionTime"].replace("Z", "+00:00"))
        out.append((end - start).total_seconds())
    return out


def stack_resources(stack: str, region: str) -> dict[str, str]:
    """Every resource of the benchmark stack, by logical ID."""
    cfn = boto3.client("cloudformation", region_name=region)
    out: dict[str, str] = {}
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        for r in page["StackResourceSummaries"]:
            out[r["LogicalResourceId"]] = r["PhysicalResourceId"]
    return out


def tracking_table(resources: dict[str, str], stack: str) -> str:
    name = resources.get("TrackingTable")
    if not name:
        raise SystemExit(f"{stack}: no TrackingTable resource")
    return name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument(
        "--common-docs",
        action="store_true",
        help="pool packet metrics over the documents every run of every value scored",
    )
    parser.add_argument(
        "--doceval-cache",
        type=Path,
        default=DOC_EVAL_CACHE,
        help="where per-document evaluations and row counts are cached",
    )
    parser.add_argument(
        "--tracking-cache",
        type=Path,
        default=TRACKING_CACHE,
        help="where tracking-table items (timings, metering) are cached",
    )
    parser.add_argument(
        "--no-per-doc",
        action="store_true",
        help="skip the per-document evaluations (no S3 reads beyond the run results)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="results file to write (default: benchmarks/results/guidance/<ID>.json)",
    )
    args = parser.parse_args()

    man = json.loads(args.manifest.read_text(encoding="utf-8"))
    resources = stack_resources(man["stack"], man["region"])
    tracking = TrackingItems(
        tracking_table(resources, man["stack"]), man["region"], args.tracking_cache
    )
    unpriced: set[str] = set()
    evals = (
        None
        if args.no_per_doc
        else DocEvals(resources, man["region"], args.doceval_cache)
    )
    isolated = set(man.get("cache_isolated") or [])
    by_doc: dict[str, dict[str, dict[str, float]]] = {}
    cells: dict[str, dict[str, float]] = {}
    usage: dict[str, list[dict[str, float]]] = {}
    long_contexts: dict[str, dict[str, Any]] = {}
    basis: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}
    sample: dict[str, dict[str, Any]] = {}
    unfinished: dict[str, dict[str, dict[str, Any]]] = {}
    run_ids: list[str] = []
    failed_total = 0
    # Each scored document's true section and page counts, per dataset: properties of
    # the document's ground truth, the same in every run that scored it.
    truth_counts: dict[str, dict[str, dict[str, int]]] = {}
    # Score every value of a dataset on the same documents: those scored in every run
    # of every value. A document one value fails on (a timeout, a content filter)
    # would otherwise count against the values that finished it.
    common: dict[str, set[str]] = {}
    # Every document some run of the dataset scored: a document a value's run is
    # missing from is one that run did not finish.
    universe: dict[str, set[str]] = {}
    for ds_key, values in man["runs"].items():
        docs: set[str] | None = None
        universe[ds_key] = set()
        for ids in values.values():
            for run_id in ids:
                path = args.results_dir / f"{run_id}-result.json"
                scores = (
                    json.loads(path.read_text(encoding="utf-8")).get(
                        "weightedOverallScores"
                    )
                    or {}
                )
                keys = {k.split("/", 1)[1] for k in scores}
                docs = keys if docs is None else docs & keys
                universe[ds_key] |= keys
        common[ds_key] = docs or set()
    for ds_key, values in man["runs"].items():
        for value, ids in values.items():
            per_doc: dict[str, list[float]] = {}
            tokens: dict[str, list[float]] = {k: [] for k in [*STEP_METRICS, "total"]}
            medians: list[float] = []
            per_run: dict[str, list[float]] = {}
            runs_usage: list[dict[str, float]] = []
            coldest: list[tuple[float, str, str, int]] = []
            scored: list[int] = []
            failures: list[int] = []
            step_lines: dict[str, list[str]] = {}
            bedrock_steps: set[str] = set()
            billed: dict[str, list[str]] = {}
            # Per-document scores, each metric averaged over the runs that scored it.
            doc_metrics: dict[str, dict[str, list[float]]] = {}
            list_fields: list[str] = []
            testsets: list[str] = []
            # Each document's cost per run, by run index, and the runs that missed it.
            doc_costs: dict[str, dict[int, float]] = {}
            missed: dict[str, list[str]] = {}
            # Each run's classification cost per 1,000 documents, by run index.
            run_classification: dict[int, float] = {}
            # Each run's long-context surcharge, and what its unsized requests could
            # add, in USD per document, in the order of runs_usage.
            run_surcharge: list[float] = []
            run_ceiling: list[float] = []
            long_context = LongContextTally()
            # How many per-run packet values each metric held before this run.
            index_runs: dict[str, int] = {}
            for index, run_id in enumerate(ids):
                path = args.results_dir / f"{run_id}-result.json"
                result = json.loads(path.read_text(encoding="utf-8"))
                failed = int(result.get("failedFiles") or 0)
                allowed = int(man.get("max_failed", {}).get(ds_key, 0))
                if failed > allowed or result.get("status") not in (
                    "COMPLETE",
                    "PARTIAL_COMPLETE",
                ):
                    raise SystemExit(
                        f"{run_id}: not a clean run ({result.get('status')})"
                    )
                failed_total += failed
                run_ids.append(run_id)
                scored.append(scored_docs(result))
                failures.append(failed)
                scores = result.get("weightedOverallScores") or {}
                for key, score in scores.items():
                    per_doc.setdefault(key.split("/", 1)[1], []).append(float(score))
                n_pages = pages(result)
                # A run Test Studio cached with an empty cost breakdown (metering not
                # yet in Athena when the result was built) has no token counts either.
                if n_pages and result.get("costBreakdown"):
                    for metric, step in STEP_METRICS.items():
                        tokens[metric].append(step_tokens(result, step) / n_pages)
                    tokens["total"].append(step_tokens(result, None) / n_pages)
                else:
                    print(
                        f"  {run_id}: no page count or usage; tokens per page left out"
                    )
                runs_usage.append(run_usage(result))
                coldest.append(
                    (
                        -cache_writes(result),
                        str(result.get("createdAt") or ""),
                        run_id,
                        index,
                    )
                )
                cfg = (result.get("config") or {}).get("Config") or {}
                if cfg:
                    run_lines, bedrock = config_models(cfg)
                    bedrock_steps |= bedrock
                    for step, text in run_lines.items():
                        seen = step_lines.setdefault(step, [])
                        if text not in seen:
                            seen.append(text)
                for step, names in billed_models(result).items():
                    billed[step] = sorted(set(billed.get(step, [])) | set(names))
                for metric, v in run_metrics(result).items():
                    per_run.setdefault(metric, []).append(v)
                # Requests are laid out by the run's own configuration, or by the
                # registry's settings for the value when the result carries none.
                layout_cfg = cfg or registry_config(str(man["ablation"]), value)
                run_evals: dict[str, dict[str, Any]] = {}
                if evals is not None:
                    field = list_field(result)
                    if field:
                        list_fields.append(field)
                    if result.get("testSetId"):
                        testsets.append(str(result["testSetId"]))
                    names = [k.split("/", 1)[1] for k in scores]
                    split_means: dict[str, list[float]] = {}
                    # With `common_docs` in the manifest, a packet metric is pooled over
                    # only the documents every run of every value scored, so values that
                    # lost different packets are compared on the same packets.
                    only_common = bool(
                        args.common_docs or man.get("common_docs")
                    ) and bool(common[ds_key])
                    pooled = {"sections": 0, "right": 0, "pages": 0, "right_pages": 0}
                    run_evals = evals.evaluations(run_id, names)
                    for doc, doc_result in run_evals.items():
                        if only_common and doc not in common[ds_key]:
                            continue
                        found = doc_metrics.setdefault(doc, {})
                        score = doc_list_score(doc_result, field) if field else None
                        if score is not None:
                            found.setdefault("list_score", []).append(score)
                        for metric, v in doc_split_scores(doc_result).items():
                            found.setdefault(metric, []).append(v)
                            split_means.setdefault(metric, []).append(v)
                        split = doc_result.get("doc_split_metrics") or {}
                        for name, count in (
                            ("sections", split.get("total_splits")),
                            ("pages", split.get("total_pages")),
                        ):
                            if count:
                                truth_counts.setdefault(name, {}).setdefault(
                                    ds_key, {}
                                )[doc] = int(count)
                        pooled["sections"] += int(split.get("total_splits") or 0)
                        pooled["right"] += int(
                            split.get("correctly_split_without_order") or 0
                        )
                        pooled["pages"] += int(split.get("total_pages") or 0)
                        pooled["right_pages"] += int(
                            split.get("correctly_classified_pages") or 0
                        )
                    # Split accuracy per run is the mean over the documents it scored,
                    # the way Test Studio's own run metrics are.
                    for metric, vals in split_means.items():
                        per_run.setdefault(metric, []).append(statistics.fmean(vals))
                    if only_common and pooled["sections"] and pooled["pages"]:
                        # Test Studio pools sections and pages over the run's documents;
                        # this pools them the same way over the common documents only.
                        for metric, pooled_value in (
                            ("section_accuracy", pooled["right"] / pooled["sections"]),
                            ("page_accuracy", pooled["right_pages"] / pooled["pages"]),
                        ):
                            vals = per_run.setdefault(metric, [])
                            if len(vals) > index_runs.get(metric, 0):
                                vals[-1] = pooled_value
                            else:
                                vals.append(pooled_value)
                    for metric in ("section_accuracy", "page_accuracy"):
                        index_runs[metric] = len(per_run.get(metric, []))
                names = [k.split("/", 1)[1] for k in scores]
                items = tracking.items(run_id, names)
                classified: list[float] = []
                surcharges: list[float] = []
                ceilings: list[float] = []
                for doc, item in items.items():
                    if isinstance(item.get(METERING_ATTRIBUTE), dict):
                        metering = item[METERING_ATTRIBUTE]
                        cost, classifying = metering_cost(
                            metering, unpriced, CLASSIFICATION_STEPS
                        )
                        evaluation = run_evals.get(doc)
                        sections = section_pages(evaluation)
                        doc_pages = document_pages(evaluation, sections, metering)
                        steps = long_context_steps(
                            metering,
                            lambda step, n=doc_pages, s=sections: request_layout(
                                step, layout_cfg, n, s
                            ),
                        )
                        long_context.add(steps)
                        extra = sum(s.surcharge for s in steps)
                        surcharges.append(extra)
                        ceilings.append(sum(s.ceiling for s in steps))
                        doc_costs.setdefault(doc, {})[index] = cost + extra
                        classified.append(
                            classifying
                            + sum(
                                s.surcharge
                                for s in steps
                                if s.step in CLASSIFICATION_STEPS
                            )
                        )
                run_surcharge.append(
                    statistics.fmean(surcharges) if surcharges else 0.0
                )
                run_ceiling.append(statistics.fmean(ceilings) if ceilings else 0.0)
                # Over the documents the run scored, as its run cost is.
                if classified:
                    run_classification[index] = 1000 * statistics.fmean(classified)
                    if len(classified) < len(names):
                        print(
                            f"  {run_id}: classification cost from {len(classified)} "
                            f"of {len(names)} documents' metering"
                        )
                for doc in sorted(universe[ds_key] - set(names)):
                    missed.setdefault(doc, []).append(run_id)
                secs = workflow_seconds(items)
                if secs:
                    medians.append(statistics.median(secs))
            cell = f"{ds_key}|{value}|"
            if common[ds_key]:
                per_doc = {k: v for k, v in per_doc.items() if k in common[ds_key]}
            if per_doc:
                cells[cell + "weighted_accuracy"] = mean_se(
                    [statistics.fmean(v) for v in per_doc.values()]
                )
            for metric in STEP_METRICS:
                if any(tokens[metric]):
                    cells[cell + metric] = mean_se(tokens[metric])
            cells[cell + "total_tokens"] = mean_se(tokens["total"])
            if medians:
                cells[cell + "median_seconds"] = mean_se(medians)
            # Test Studio sometimes leaves a run's packet metrics empty; two runs are
            # enough for a mean and an SE, so a cell needs all its runs but one.
            for metric, vals in per_run.items():
                if len(vals) >= max(2, len(ids) - 1):
                    cells[cell + metric] = mean_se(vals)
            key = f"{ds_key}|{value}"
            all_isolated = bool(ids) and all(i in isolated for i in ids)
            # A document's cost follows the run cost's basis: every run when the runs
            # are cache-isolated, otherwise the coldest run alone.
            cold_index = min(coldest)[3] if coldest else 0
            # Classification cost follows the same basis: the mean over every run
            # when the runs are cache-isolated, otherwise the coldest run alone.
            if all_isolated and run_classification:
                cells[cell + CLASSIFICATION_COST] = mean_se(
                    [run_classification[i] for i in sorted(run_classification)]
                )
            elif cold_index in run_classification:
                cells[cell + CLASSIFICATION_COST] = {
                    "mean": round(run_classification[cold_index], 6),
                    "se": 0.0,
                }
            if evals is not None and doc_metrics:
                shown = [
                    d
                    for d in sorted(doc_metrics)
                    if not common[ds_key] or d in common[ds_key]
                ]
                field = statistics.mode(list_fields) if list_fields else ""
                rows = (
                    evals.rows(statistics.mode(testsets), shown, field)
                    if field and testsets
                    else {}
                )
                entries: dict[str, dict[str, float]] = {}
                for doc in shown:
                    found = doc_metrics[doc]
                    entry: dict[str, float] = {}
                    if "list_score" in found:
                        entry["list_score"] = round(
                            statistics.fmean(found["list_score"]), 6
                        )
                        if doc in rows:
                            entry["list_rows"] = rows[doc]
                    for metric in DOC_SPLIT_METRICS:
                        if metric in found:
                            entry[metric] = round(statistics.fmean(found[metric]), 6)
                    if doc in per_doc:
                        entry["weighted_accuracy"] = round(
                            statistics.fmean(per_doc[doc]), 6
                        )
                    cost = doc_cost_over_runs(
                        doc_costs.get(doc) or {}, all_isolated, cold_index
                    )
                    if cost is not None:
                        entry["cost"] = round(cost, 6)
                    if entry:
                        entries[doc] = entry
                if entries:
                    by_doc[key] = entries
            if missed:
                field = statistics.mode(list_fields) if list_fields else ""
                rows = (
                    evals.rows(statistics.mode(testsets), sorted(missed), field)
                    if evals is not None and field and testsets
                    else {}
                )
                found_missed: dict[str, dict[str, Any]] = {}
                for doc, run_list in sorted(missed.items()):
                    statuses = {
                        str(item.get("ObjectStatus") or "UNKNOWN")
                        for run_id in run_list
                        for item in [tracking.items(run_id, [doc]).get(doc) or {}]
                    }
                    found_missed[doc] = {
                        "runs": len(run_list),
                        "of": len(ids),
                        **({"list_rows": rows[doc]} if doc in rows else {}),
                        "status": sorted(statuses),
                    }
                unfinished[key] = found_missed
            if scored:
                sample[key] = {"docs": scored, "failed": failures, "runs": len(ids)}
            if step_lines:
                lines = {
                    s: " ; ".join(step_lines[s])
                    for s in STEP_BILLING
                    if s in step_lines
                }
                with_billed(lines, bedrock_steps, billed)
                models[key] = {**lines, "billed": dict(sorted(billed.items()))}
            if all_isolated and any(runs_usage) and not all(runs_usage):
                # Test Studio occasionally records no metering for a run. With
                # cache-isolated runs every run is priced alike, so price the
                # value from the runs that have usage.
                kept = [i for i, u in enumerate(runs_usage) if u]
                runs_usage = [runs_usage[i] for i in kept]
                run_surcharge = [run_surcharge[i] for i in kept]
                run_ceiling = [run_ceiling[i] for i in kept]
            if long_context.total["documents"]:
                long_contexts[key] = {
                    "rules": {
                        m: {
                            "threshold": LONG_CONTEXT_RULES[m].threshold,
                            "multiplier": LONG_CONTEXT_RULES[m].multiplier,
                        }
                        for m in sorted(long_context.models)
                    },
                    **long_context.total,
                    "by_step": dict(sorted(long_context.by_step.items())),
                    "surcharge": [round(v, 8) for v in run_surcharge],
                    "unsized_ceiling": [round(v, 8) for v in run_ceiling],
                }
            if runs_usage and all(runs_usage):
                usage[key] = runs_usage
                if all_isolated:
                    basis[key] = {"mode": "cache-isolated"}
                else:
                    _, _, cold_id, cold_index = min(coldest)
                    basis[key] = {
                        "mode": "cold-run",
                        "run": cold_id,
                        "index": cold_index,
                    }

    out = {
        "release": man["release"],
        "measured": man["measured"],
        "region": man["region"],
        "source": "Test Studio runs "
        + ", ".join(run_ids)
        + (
            f" ({failed_total} failed documents left out of the scores)"
            if failed_total
            else ""
        ),
        "verdict": man["verdict"],
        "cells": cells,
        "sample": sample,
        "models": models,
        "usage": usage,
        "cost_basis": basis,
    }
    if long_contexts:
        out["long_context"] = long_contexts
    if by_doc:
        out["per_doc"] = by_doc
    if unfinished:
        out["unfinished"] = unfinished
    for name, by_ds in sorted(truth_counts.items()):
        out[f"{name}_per_doc"] = {
            ds_key: round(statistics.fmean(counts.values()), 4)
            for ds_key, counts in by_ds.items()
            if counts
        }
    path = args.out or OUT / f"{man['ablation']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(REPO) if path.is_relative_to(REPO) else path}")
    for k, v in cells.items():
        print(f"  {k}: {v['mean']:.4g} ± {2 * v['se']:.2g}")
    for k, lc in long_contexts.items():
        print(
            f"  {k}: long-context rate on {lc['long_requests']} of {lc['requests']} "
            f"sized requests, {lc['long_documents']} of {lc['documents']} document "
            f"runs; {lc['unsized_documents']} unsized over the threshold"
        )
    for k, entries in by_doc.items():
        with_list = [d for d in entries.values() if "list_score" in d]
        print(f"  {k}: {len(entries)} documents, {len(with_list)} with a list score")
    for k, late in unfinished.items():
        print(f"  {k}: {len(late)} documents some run did not finish")
    for name in ("sections_per_doc", "pages_per_doc"):
        for ds_key, count in (out.get(name) or {}).items():
            print(f"  {ds_key}: {count:g} {name.replace('_', ' ')}")
    for name in sorted(unpriced):
        print(f"  warning: no price for {name}; counted as $0", file=sys.stderr)
    if evals is not None and evals.missing:
        for name in sorted(evals.missing)[:5]:
            print(f"  no per-document evaluation at {name}", file=sys.stderr)
        print(
            f"  {len(evals.missing)} per-document files could not be read",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
