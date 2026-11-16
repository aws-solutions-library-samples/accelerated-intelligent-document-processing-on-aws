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
    page_accuracy       share of pages with the right class, per run; SE across runs
    section_accuracy    share of sections with the right pages and class, per run
    grouping_score      DocSplit packet score for grouping pages into documents

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
        "label_accuracy": 1.0
    }}}

`boundary_accuracy` and `label_accuracy` are also written as per-run cells, each the
mean over the documents that run scored, because Test Studio's run-level metrics do
not include them.

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

Usage:
    AWS_PROFILE=... python3 benchmarks/harness/collect_guidance_results.py \\
        manifest.json --results-dir /tmp/ex01/results
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3

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
DOC_EVAL_CACHE = Path("/tmp/guidance-runs/doceval")
DOC_EVAL_WORKERS = 8
#: The review budget behind `errors_caught_30`: the share of values a reviewer checks,
#: lowest confidence first, as Test Studio keys it under error_capture_at_budget.
REVIEW_BUDGET = "0.30"
#: Cost-breakdown services kept as usage, and units that carry no price of their own.
PRICED_SERVICES = ("bedrock/", "textract/")
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


def mean_se(values: list[float]) -> dict[str, float]:
    m = statistics.fmean(values)
    se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return {"mean": round(m, 6), "se": round(se, 6)}


def model_name(model_id: Any) -> str:
    """The display name of a model ID, or the raw ID when we have no name for it."""
    if not model_id:
        return ""
    raw = str(model_id)
    base = raw.removesuffix(":1m")
    for prefix in REGION_PREFIXES:
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break
    return MODEL_NAMES.get(base, raw)


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
            service_id, _, unit = key.rpartition("_")
            if not service_id or unit in UNPRICED_UNITS:
                continue
            name = f"{service_id}|{unit}"
            totals[name] = totals.get(name, 0.0) + float(item.get("value") or 0)
    return {k: round(v / docs, 4) for k, v in sorted(totals.items())}


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
    return int(result.get("splitClassificationMetrics", {}).get("total_pages") or 0)


def workflow_seconds(table: Any, run_id: str, doc_keys: list[str]) -> list[float]:
    out = []
    for key in doc_keys:
        item = table.get_item(Key={"PK": f"doc#{key}", "SK": "none"}).get("Item")
        if (
            not item
            or not item.get("CompletionTime")
            or not item.get("WorkflowStartTime")
        ):
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


def tracking_table(resources: dict[str, str], stack: str, region: str) -> Any:
    name = resources.get("TrackingTable")
    if not name:
        raise SystemExit(f"{stack}: no TrackingTable resource")
    return boto3.resource("dynamodb", region_name=region).Table(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument(
        "--doceval-cache",
        type=Path,
        default=DOC_EVAL_CACHE,
        help="where per-document evaluations and row counts are cached",
    )
    parser.add_argument(
        "--no-per-doc",
        action="store_true",
        help="skip the per-document evaluations (no S3 reads beyond the run results)",
    )
    args = parser.parse_args()

    man = json.loads(args.manifest.read_text(encoding="utf-8"))
    resources = stack_resources(man["stack"], man["region"])
    table = tracking_table(resources, man["stack"], man["region"])
    evals = (
        None
        if args.no_per_doc
        else DocEvals(resources, man["region"], args.doceval_cache)
    )
    isolated = set(man.get("cache_isolated") or [])
    by_doc: dict[str, dict[str, dict[str, float]]] = {}
    cells: dict[str, dict[str, float]] = {}
    usage: dict[str, list[dict[str, float]]] = {}
    basis: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}
    sample: dict[str, dict[str, Any]] = {}
    run_ids: list[str] = []
    failed_total = 0
    # Score every value of a dataset on the same documents: those scored in every run
    # of every value. A document one value fails on (a timeout, a content filter)
    # would otherwise count against the values that finished it.
    common: dict[str, set[str]] = {}
    for ds_key, values in man["runs"].items():
        docs: set[str] | None = None
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
                n_pages = pages(result) or 1
                for metric, step in STEP_METRICS.items():
                    tokens[metric].append(step_tokens(result, step) / n_pages)
                tokens["total"].append(step_tokens(result, None) / n_pages)
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
                if evals is not None:
                    field = list_field(result)
                    if field:
                        list_fields.append(field)
                    if result.get("testSetId"):
                        testsets.append(str(result["testSetId"]))
                    names = [k.split("/", 1)[1] for k in scores]
                    split_means: dict[str, list[float]] = {}
                    for doc, doc_result in evals.evaluations(run_id, names).items():
                        found = doc_metrics.setdefault(doc, {})
                        score = doc_list_score(doc_result, field) if field else None
                        if score is not None:
                            found.setdefault("list_score", []).append(score)
                        for metric, v in doc_split_scores(doc_result).items():
                            found.setdefault(metric, []).append(v)
                            split_means.setdefault(metric, []).append(v)
                    # Split accuracy per run is the mean over the documents it scored,
                    # the way Test Studio's own run metrics are.
                    for metric, vals in split_means.items():
                        per_run.setdefault(metric, []).append(statistics.fmean(vals))
                secs = workflow_seconds(table, run_id, list(scores))
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
            for metric, vals in per_run.items():
                if len(vals) == len(ids):
                    cells[cell + metric] = mean_se(vals)
            key = f"{ds_key}|{value}"
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
                    if entry:
                        entries[doc] = entry
                if entries:
                    by_doc[key] = entries
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
            all_isolated = bool(ids) and all(i in isolated for i in ids)
            if all_isolated and any(runs_usage) and not all(runs_usage):
                # Test Studio occasionally records no metering for a run. With
                # cache-isolated runs every run is priced alike, so price the
                # value from the runs that have usage.
                runs_usage = [u for u in runs_usage if u]
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
    if by_doc:
        out["per_doc"] = by_doc
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{man['ablation']}.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(REPO)}")
    for k, v in cells.items():
        print(f"  {k}: {v['mean']:.4g} ± {2 * v['se']:.2g}")
    for k, entries in by_doc.items():
        with_list = [d for d in entries.values() if "list_score" in d]
        print(f"  {k}: {len(entries)} documents, {len(with_list)} with a list score")
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
