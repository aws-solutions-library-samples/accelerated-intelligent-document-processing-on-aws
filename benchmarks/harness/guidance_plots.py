"""Draw the plots for the "Benchmarks by Problem Set" pages.

render_guidance.py calls one of two functions for each ablation that has measured cells:

- draw_scatter() when the ablation reports cost or tokens: a score against cost per
  1,000 documents (or tokens per page), one panel per dataset, a point per value with
  a soft band spanning two standard errors either side. The title is the takeaway and
  one muted line under it says what varies and what is held ("Everything else:
  recommended setup", with the baseline spelled out in a footnote). Each panel has its
  own padded x range, always from $0; a metric's panels share a y range unless that
  would flatten one. Each point is named directly, beside it or with a leader of at
  most MAX_LEADER pixels, clear of other labels, points, error bars and reference
  lines; points drawn on top of each other share one label. Only a point that still
  finds no spot is numbered and keyed under the chart, and a panel of four or fewer
  labels is never numbered (its labels are shortened instead). The reference point (the registry's
  `reference`, else the default value) is drawn and labelled bold. A metric with a
  baseline gets it as a dashed line. When the values are models, each point takes its
  family's color and shape, listed in a legend.
- draw_by_size() for an ablation with a `by_size` key: one per-document score by size
  band (the registry's `strata.bands`), one point per band per value at the mean with
  a two-standard-error bar, lines joining a value's bands, and N under each band.
- draw_plot() otherwise: one metric, a panel per dataset, a row per value, a dot at the
  mean and a band spanning two standard errors either side.

Axis titles carry the direction that is better (↑ or ←), so no chart needs a
"better" note. Each SVG is static (no script), follows the light or dark color scheme,
and carries a <title> (the takeaway) and a <desc> for screen readers; the results
table under it holds every number.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
from functools import cache
from html import escape
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "benchmarks" / "matrices" / "guidance_ablations.yaml"


@cache
def _registry() -> dict[str, Any]:
    try:
        reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    return _interim_overlay(reg)


def _interim_overlay(reg: dict[str, Any]) -> dict[str, Any]:
    # Interim results: a results file carrying `interim_ablation` (a full registry
    # entry) was measured against that entry, not the current one, so the entry it
    # carries replaces the registry's until a re-run overwrites the results file.
    ablations = reg.get("ablations") or []
    for i, ab in enumerate(ablations):
        entry = _results(ab["id"]).get("interim_ablation")
        if isinstance(entry, dict):
            # The reference is the registry's when the interim entry names none and
            # the interim run measured it, so a "(default)" point is not drawn as one.
            ref = ab.get("reference")
            if not entry.get("reference") and ref in (entry.get("values") or []):
                entry = {**entry, "reference": ref}
            ablations[i] = entry
    return reg


def _ablation(title: str, values: list[str] | None = None) -> dict[str, Any] | None:
    """The registry entry a chart is drawn for, found by its title (and values).

    A chart's title is its ablation's takeaway, details takeaway or question, so the
    title names the ablation; the values narrow it when two share a question. The
    entry says which value is the reference point, which baseline the other values
    start from, and how a by-size chart is banded.
    """
    if not title:
        return None
    found = [
        ab
        for ab in _registry().get("ablations") or []
        if title in (ab.get("takeaway"), ab.get("details_takeaway"), ab.get("question"))
    ]
    if values and len(found) > 1:
        found = [ab for ab in found if set(values) <= set(ab.get("values") or [])]
    return found[0] if found else None


def _baseline(ab: dict[str, Any] | None) -> dict[str, Any] | None:
    name = (ab or {}).get("baseline")
    return ((_registry().get("baselines") or {}).get(name) or None) if name else None


def _chart_title(title: str, ab: dict[str, Any] | None) -> str:
    """The takeaway in place of the question, whenever the ablation has one."""
    if not ab or title != ab.get("question"):
        return title
    if ab.get("by_size") and ab.get("details_takeaway"):
        return str(ab["details_takeaway"])
    return str(ab.get("takeaway") or title)


#: The step a page's question turns on: (results `models` key, registry keys).
PAGE_STEPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "extraction": ("Extraction", ("extraction.model",)),
    "digitization": ("Extraction", ("extraction.model",)),
    "token-use": ("Extraction", ("extraction.model",)),
    "splitting-and-classification": (
        "Classification",
        (
            "classification.combined.llm.model",
            "classification.split_then_classify.splitting.llm.model",
            "classification.split_then_classify.section_classification.llm.model",
        ),
    ),
    "confidence-assessment": ("Confidence", ("extraction.confidence.model",)),
}
#: Title words that keep their capital after "With <model>, ".
PROPER_WORDS = frozenset({"Amazon", "Claude", "Textract", "Layout", "OCR"})
MAX_TITLE_WORDS = 14


def _step_names(text: str) -> set[str]:
    """The models a results `models` entry names for its step: the primary one and,
    for split-then-classify, the classifier ("Claude Haiku 5.5, holistic" gives
    {"Claude Haiku 5.5"}); escalation targets are not the step's model."""
    if not text or text == "Off":
        return set()
    primary = re.sub(r"\s*\([^()]*\)|\s+finds the sections.*", "", text.split(", ")[0])
    names = {primary.strip()}
    then = re.search(r"\bthen (.+?) classifies\b", text)
    if then:
        names.add(then.group(1).strip())
    return {n for n in names if n}


def _single_model(ab: dict[str, Any] | None, values: list[str]) -> str:
    """The one model every drawn value ran for the step its page's question turns on
    (extraction, classification or scoring), or "" when the values differ there.

    The results' `models` say what ran; without them, the registry's baseline and each
    value's `set` say what was configured. A value with that step off counts for none.
    """
    if not ab or not values:
        return ""
    step, keys = PAGE_STEPS.get(str(ab.get("page") or ""), ("", ()))
    if not step:
        return ""
    found: set[str] = set()
    seen = False
    for k, m in _results_models(str(ab["id"])).items():
        if k.split("|")[1:2] and k.split("|")[1] in values:
            seen = True
            found |= _step_names(str(m.get(step) or ""))
    if not seen:
        base = (_baseline(ab) or {}).get("set") or {}
        sets = dict(zip(ab.get("values") or [], ab.get("set") or [], strict=False))
        for v in values:
            s = {**base, **(sets.get(v) or {})}
            ids = {s.get(key) for key in keys}
            if None in ids or len(ids) != 1:
                return ""
            found.add(_model_name(str(ids.pop())))
    return found.pop() if len(found) == 1 else ""


def _model_title(title: str, model: str) -> str:
    """The title with the one model it was measured on, unless it names it already:
    "With Claude Haiku 5.5, layout only ...", or "Claude Haiku 5.5: ..." when the
    longer form would run past MAX_TITLE_WORDS."""
    if not title or not model:
        return title
    short = re.sub(r"^(?:Amazon|Claude|OpenAI|xAI) ", "", model)
    if short in title:
        return title
    first, _, rest = title.partition(" ")
    if first not in PROPER_WORDS and not _names_model(first) and first[1:].islower():
        first = first.lower()
    lowered = f"{first} {rest}".strip()
    long = f"With {model}, {lowered}"
    if len(long.split()) <= MAX_TITLE_WORDS or ":" in title:
        return long
    return f"{model}: {lowered}"


def _held_model(ab: dict[str, Any] | None, key: str = "extraction.model") -> str:
    """The model every value of the ablation sets for `key`, or ""."""
    sets = (ab or {}).get("set") or []
    ids = {s.get(key) for s in sets if isinstance(s, dict)}
    return str(ids.pop()) if len(sets) and len(ids) == 1 and None not in ids else ""


def _model_name(model_id: str) -> str:
    """A model's name, from a registry value that sets it, else from its ID."""
    for ab in _registry().get("ablations") or []:
        for value, s in zip(ab.get("values") or [], ab.get("set") or [], strict=False):
            if (
                isinstance(s, dict)
                and len(s) == 1
                and model_id in s.values()
                and _names_model(value)
            ):
                return re.sub(r"\s*\([^()]*\)", "", value).strip()
    bare = re.sub(r"^(?:us|eu|apac|global|us-gov)\.", "", model_id.split(":")[0])
    vendor, _, name = bare.partition(".")
    words: list[str] = []
    for w in re.sub(r"-v\d+$", "", name).split("-"):
        if w.isdigit() and words and words[-1].replace(".", "").isdigit():
            words[-1] += f".{w}"
        else:
            words.append(w.upper() if w == "gpt" else w[:1].upper() + w[1:])
    prefix = {"amazon": "Amazon ", "openai": "OpenAI ", "xai": "xAI "}.get(vendor, "")
    return prefix + " ".join(words)


def _prices_note(ab: dict[str, Any] | None) -> str:
    """The price caveat every chart showing a cost carries, as render_guidance says it."""
    import sys

    region = str(_results(str((ab or {}).get("id") or "")).get("region") or "")
    for name in ("__main__", "render_guidance", "benchmarks.harness.render_guidance"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "price_caveat_svg"):
            return str(mod.price_caveat_svg(region))
    return ""


@cache
def _results(ablation_id: str) -> dict[str, Any]:
    """An ablation's results file, read from where render_guidance.py reads it."""
    root = Path(
        os.environ.get("GUIDANCE_RESULTS_DIR")
        or REPO / "benchmarks" / "results" / "guidance"
    )
    try:
        return json.loads((root / f"{ablation_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _results_models(ablation_id: str) -> dict[str, Any]:
    """The results file's models per "dataset|value", to name a point's model."""
    return _results(ablation_id).get("models") or {}


def _series_value(label: str, values: list[str]) -> str | None:
    """The value a series label names: the value itself, or the value without its
    model's vendor and any note in parentheses ("Agentic · Sonnet 5.5")."""

    def bare(t: str) -> str:
        return re.sub(r"\s*\([^()]*\)", "", VENDOR.sub("", t)).strip()

    return next((v for v in values if v == label), None) or next(
        (v for v in values if bare(v) == bare(label)), None
    )


def _doc_costs(
    ab: dict[str, Any] | None, series: list[tuple[str, list[tuple[str, float, float]]]]
) -> list[dict[str, float]]:
    """Each series' cost per document, by document, from the results' `per_doc`.

    A series is mapped to its value by name (_series_value). Raises ValueError when a
    series has costs for fewer than 90% of its documents, so a wrong mapping cannot
    draw a dot at the wrong size.
    """
    if not ab:
        return [{} for _ in series]
    per_doc = _results(str(ab["id"])).get("per_doc") or {}
    if not per_doc:
        return [{} for _ in series]
    out = []
    for label, pts in series:
        value = _series_value(label, list(ab.get("values") or []))
        if value is None:
            raise ValueError(f"{ab['id']}: by-size series {label!r} names no value")
        # The dataset that holds most of the series' documents for this value.
        entries = sorted(
            (e for k, e in per_doc.items() if k.split("|")[1:2] == [value]),
            key=lambda e: -sum(doc in e for doc, _, _ in pts),
        )
        costs: dict[str, float] = {}
        for doc, _, _ in pts:
            e = next((e[doc] for e in entries if doc in e), None)
            if e and e.get("cost") is not None:
                costs[doc] = float(e["cost"])
        if pts and len(costs) < 0.9 * len(pts):
            raise ValueError(
                f"{ab['id']}: {value}: cost for {len(costs)} of {len(pts)} documents"
            )
        out.append(costs)
    return out


#: A model's vendor, which a point's label leaves out: the legend carries the family.
VENDOR = re.compile(
    r"\b(?:Amazon|Claude|OpenAI|xAI) (?=(?:Nova|Haiku|Sonnet|Opus|GPT|Grok)\b)"
)


def _names_model(text: str) -> bool:
    return bool(re.search(r"\b(?:Nova|Haiku|Sonnet|Opus|GPT|Grok)\b", text))


def _extraction_model(ablation_id: str, value: str) -> str:
    """The extraction model a value ran, from its results ("Sonnet 5"), or ""."""
    names = {
        VENDOR.sub("", str(m.get("Extraction") or "").split(", ")[0]).strip()
        for k, m in _results_models(ablation_id).items()
        if k.split("|")[1:2] == [value] and m.get("Extraction")
    }
    return names.pop() if len(names) == 1 else ""


#: Metrics where 1 - y is a share of errors, so the marginal cost of error applies.
ERROR_RATE_METRICS = frozenset(
    {
        "weighted_accuracy",
        "all_fields_right",
        "page_accuracy",
        "section_accuracy",
        "completeness",
        "cell_accuracy",
        "list_recall",
        "list_precision",
    }
)

#: The width the docs site lays the chart out at on a desktop, so text is drawn 1:1.
WIDTH = 792
LABEL_W = 260
PLOT_X0 = LABEL_W + 20
PLOT_X1 = WIDTH - 28
ROW_H = 26
ROW_H_WRAPPED = 38
PANEL_TITLE_H = 30
AXIS_H = 26
PANEL_GAP = 18
TOP = 8
CHAR_W = 6.7  # average advance of 12px system-ui, used to wrap value labels
MAX_CHARS = int(LABEL_W / CHAR_W)
SUB_CHAR_W = 5.5  # measured advance of 11px system-ui, to wrap the models line
SUB_LINE_H = 13
PRICES_H = 16
#: Half the width of the band behind a dot, in pixels.
BAND = 5

STYLE = """
  .viz { --series: #2a78d6; --ring: #ffffff; --ink: #0b0b0b; --ink-2: #52514e;
         --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7;
         --fam-nova: #2a78d6; --fam-claude: #eb6834; --fam-other: #1baf7a; }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz, svg.viz:root { --series: #3987e5;
      --ring: #17181c; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --axis: #383835;
      --fam-nova: #3987e5; --fam-claude: #d95926; --fam-other: #199e70; }
  }
  :root[data-theme="dark"] .viz { --series: #3987e5; --ring: #17181c; --ink: #f0efec;
    --ink-2: #c3c2b7; --muted: #898781; --grid: #2c2c2a; --axis: #383835;
    --fam-nova: #3987e5; --fam-claude: #d95926; --fam-other: #199e70; }
  .viz { max-width: 100%; height: auto; }
  text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .viz .ctitle { font-size: 15px; font-weight: 600; fill: var(--ink); }
  .panel { font-size: 13px; font-weight: 600; fill: var(--ink); }
  .sub { font-size: 12px; fill: var(--muted); }
  .viz .lead { font-size: 12px; fill: var(--ink); }
  .label { font-size: 12px; fill: var(--ink-2); }
  .viz .plabel { font-size: 12px; fill: var(--ink-2); }
  .viz .plabel.dflt, .viz .plabel .dflt { fill: var(--ink); font-weight: 700; }
  .viz .dot.ref { stroke: var(--ink); stroke-width: 1.5; }
  .tick { font-size: 11px; fill: var(--muted); font-variant-numeric: tabular-nums; }
  .none { font-size: 12px; fill: var(--muted); }
  .grid { stroke: var(--grid); stroke-width: 1; }
  .axis { stroke: var(--ink-2); stroke-width: 1; }
  .viz .gap { fill: var(--ring); }
  .viz .leader { stroke: var(--muted); stroke-width: 1; }
  .band { fill: var(--series); opacity: 0.12; }
  .dot { fill: var(--series); stroke: var(--ring); stroke-width: 2; }
  .axislabel { font-size: 12px; fill: var(--ink-2); }
  .prices { font-size: 10px; fill: var(--muted); }
  .num { font-size: 11px; font-weight: 700; fill: var(--ink); }
  .halo { paint-order: stroke; stroke: var(--ring); stroke-width: 3px;
          stroke-linejoin: round; }
  .mcoe { stroke: var(--ink-2); stroke-width: 1; fill: none; }
  .mcoelabel { font-size: 11px; fill: var(--ink-2); }
  .viz .ptitle { font-size: 12px; font-weight: 600; fill: var(--ink); }
  .viz .fam-nova { fill: var(--fam-nova); }
  .viz .fam-claude { fill: var(--fam-claude); }
  .viz .fam-other { fill: var(--fam-other); }
  .viz .baseline { stroke: var(--ink-2); stroke-width: 1; stroke-dasharray: 5 4; }
  .viz .baselinelabel { font-size: 11px; fill: var(--ink-2); }
"""


def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """Return round tick values covering [lo, hi]."""
    if hi <= lo:
        hi = lo + (abs(lo) or 1.0)
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = []
    t = start
    while t <= hi + step * 1e-9:
        ticks.append(round(t, 10))
        t += step
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def tick_decimals(ticks: list[float]) -> int:
    """The fewest decimals that print every tick exactly (0.25 needs two, not one)."""
    return next(
        (d for d in range(5) if all(abs(round(t, d) - t) < 1e-9 for t in ticks)), 4
    )


def fmt(v: float) -> str:
    """Format a tick or mean the way the tables do."""
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.0f}"
    return f"{v:.2f}".rstrip("0").rstrip(".") if v != 0 else "0"


def money(v: float) -> str:
    """US dollars: $X.XX under $10, $X.X under $100, else $X,XXX."""
    a = abs(v)
    text = f"{a:.2f}" if a < 10 else f"{a:.1f}" if a < 100 else f"{a:,.0f}"
    return ("-" if v < 0 else "") + "$" + text


def marginal_cost_of_error(
    points: list[tuple[str, dict[str, float], dict[str, float]]],
) -> tuple[str, dict[str, float | None]]:
    """Return the cheapest value and the marginal cost of error of each other value.

    points: [(value, cost per 1,000 documents, accuracy), ...], each {"mean", "se"}.
    The reference is the cheapest value (ties: the less accurate one). For each value
    more accurate than it, the result is the extra cost per document divided by the
    drop in error rate, in US dollars, or None when the gain is within two standard
    errors of the difference. Values no more accurate than the reference are left out.
    """
    ref, rx, ry = min(points, key=lambda p: (p[1]["mean"], p[2]["mean"]))
    out: dict[str, float | None] = {}
    for value, x, y in points:
        gain = y["mean"] - ry["mean"]
        if value == ref or gain <= 0:
            continue
        noise = 2 * math.hypot(ry.get("se", 0), y.get("se", 0))
        out[value] = (x["mean"] - rx["mean"]) / 1000 / gain if gain > noise else None
    return ref, out


def mcoe_text(cost: float | None, *, short: bool = False) -> str:
    """The marginal cost of error in words; `short` for a label on a chart's line."""
    if cost is None:
        return "no measurable gain"
    return f"{money(cost)} per error" + ("" if short else " prevented")


def wrap(text: str) -> list[str]:
    """Wrap a value label onto at most two lines, ellipsizing the second."""
    if len(text) <= MAX_CHARS:
        return [text]
    words, first = text.split(), ""
    while words and len(first) + len(words[0]) + (1 if first else 0) <= MAX_CHARS:
        first = f"{first} {words.pop(0)}".strip()
    rest = " ".join(words)
    if len(rest) > MAX_CHARS:
        rest = rest[: MAX_CHARS - 1].rstrip() + "…"
    return [first, rest] if first else [text[: MAX_CHARS - 1] + "…"]


def wrap_subtitle(text: str, width: float = WIDTH) -> list[str]:
    """Wrap the models line, breaking between steps first so nothing is clipped."""
    if not text:
        return []
    limit = max(20, int(width / SUB_CHAR_W) - 2)  # room for a trailing " ·"
    lines: list[str] = []
    current = ""
    for part in text.split(" · "):
        joined = f"{current} · {part}" if current else part
        if current and len(joined) > limit:
            lines.append(current)
            current = part
        else:
            current = joined
    if current:
        lines.append(current)
    out: list[str] = []
    for n, line in enumerate(lines):
        pieces: list[str] = []
        while len(line) > limit:  # one step longer than the width still has to break
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(line[:cut])
            line = line[cut:].lstrip()
        pieces.append(line)
        if n < len(lines) - 1:  # the separator stays at the end of the line it broke
            pieces[-1] += " ·"
        out += pieces
    return out


#: The models under a panel title: (lead, subtitle). The lead is one line in primary
#: ink naming the model for the page's own step; the subtitle names every step and N.
Models = str | tuple[str, str]
LEAD_H = 15


def models_svg(models: Models, baseline: float) -> tuple[str, float]:
    """Render the lead line and the wrapped subtitle; return the SVG and its height."""
    lead, sub = models if isinstance(models, tuple) else ("", models)
    out, height = "", 0.0
    if lead:
        out = f'<text class="lead" x="0" y="{baseline + 15:.1f}">{escape(lead)}</text>'
        height = LEAD_H
    sub_svg, sub_h = subtitle_svg(wrap_subtitle(sub), baseline + height)
    return out + sub_svg, height + sub_h


def subtitle_svg(lines: list[str], baseline: float) -> tuple[str, float]:
    """Render the models line under a panel title; return the SVG and its height."""
    if not lines:
        return "", 0.0
    spans = "".join(
        f'<tspan x="0" y="{baseline + 14 + i * SUB_LINE_H:.1f}">{escape(line)}</tspan>'
        for i, line in enumerate(lines)
    )
    return f'<text class="sub">{spans}</text>', SUB_LINE_H * len(lines)


def prices_svg(prices: str, y: float) -> str:
    return (
        f'<text class="prices" x="0" y="{y:.1f}">{escape(prices)}</text>'
        if prices
        else ""
    )


def draw_plot(
    metric_name: str,
    panels: list[tuple[str, Models, list[tuple[str, dict[str, float] | None]]]],
    runs: int,
    *,
    prices: str = "",
    ratio: bool = False,
) -> str:
    """Return an SVG document.

    panels: [(dataset name, models line, [(value label, {"mean", "se"} or None), ...])]
    prices: a price-list note for a chart that shows cost, or "" for one that does not.
    ratio: the metric is a multiple (RATIO_METRICS), written "2.5×".
    """
    points = [p for _, _, rows in panels for _, p in rows if p]
    lo = min(p["mean"] - 2 * p.get("se", 0) for p in points)
    hi = max(p["mean"] + 2 * p.get("se", 0) for p in points)
    pad = (hi - lo) * 0.08 or abs(hi) * 0.05 or 1.0
    ticks = nice_ticks(lo - pad, hi + pad)
    d0, d1 = ticks[0], ticks[-1]

    decimals = tick_decimals(ticks)
    num = times if ratio else fmt

    def tick_text(t: float) -> str:
        return times(t, max(1, decimals)) if ratio else f"{t:,.{decimals}f}"

    def x(v: float) -> float:
        return PLOT_X0 + (v - d0) / (d1 - d0) * (PLOT_X1 - PLOT_X0)

    body: list[str] = []
    y = TOP
    for ds_name, models, rows in panels:
        body.append(f'<text class="panel" x="0" y="{y + 18}">{escape(ds_name)}</text>')
        sub, sub_h = models_svg(models, y + 18)
        body.append(sub)
        y += PANEL_TITLE_H + sub_h
        top = y
        heights = [ROW_H_WRAPPED if len(wrap(v)) > 1 else ROW_H for v, _ in rows]
        bottom = top + sum(heights)
        for t in ticks:
            body.append(
                f'<line class="grid" x1="{x(t):.1f}" x2="{x(t):.1f}" '
                f'y1="{top}" y2="{bottom}"/>'
            )
        for (value, p), h in zip(rows, heights):
            cy = y + h / 2
            lines = wrap(value)
            first_y = cy + 4 - (len(lines) - 1) * 7
            tspans = "".join(
                f'<tspan x="0" y="{first_y + i * 14:.1f}">{escape(line)}</tspan>'
                for i, line in enumerate(lines)
            )
            body.append(f'<text class="label">{tspans}</text>')
            if p is None:
                body.append(
                    f'<text class="none" x="{PLOT_X0}" y="{cy + 4:.1f}">'
                    "Not measured</text>"
                )
            else:
                m, s = p["mean"], p.get("se", 0)
                body.append(
                    f"<g><title>{escape(value)}: {num(m)} ± {num(2 * s)}</title>"
                    f'<rect class="band" x="{x(m - 2 * s):.1f}" '
                    f'y="{cy - BAND:.1f}" width="{x(m + 2 * s) - x(m - 2 * s):.1f}" '
                    f'height="{2 * BAND}" rx="{BAND}"/>'
                    f'<circle class="dot" cx="{x(m):.1f}" cy="{cy:.1f}" r="5"/></g>'
                )
                body.append(
                    f'<text class="label" x="{x(m + 2 * s) + 8:.1f}" y="{cy + 4:.1f}">'
                    f"{num(m)}</text>"
                )
            y += h
        body.append(
            f'<line class="axis" x1="{PLOT_X0}" x2="{PLOT_X1}" '
            f'y1="{bottom}" y2="{bottom}"/>'
        )
        for t in ticks:
            body.append(
                f'<text class="tick" x="{x(t):.1f}" y="{bottom + 16}" '
                f'text-anchor="middle">{tick_text(t)}</text>'
            )
        y = bottom + AXIS_H + PANEL_GAP
    height = y - PANEL_GAP + (PRICES_H if prices else 0)
    body.append(prices_svg(prices, height - 5))

    desc = "; ".join(
        f"{ds}: "
        + ", ".join(
            f"{v} {num(p['mean'])}" if p else f"{v} not measured" for v, p in rows
        )
        for ds, _, rows in panels
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" width="{WIDTH}" '
        f'height="{height}" viewBox="0 0 {WIDTH} {height}" role="img">'
        f"<title>{escape(metric_name)}, mean of {runs} runs with two standard "
        f"errors either side</title><desc>{escape(desc)}</desc>"
        f"<style>{STYLE}</style>" + "".join(body) + "</svg>\n"
    )


#: Share metrics: a score between 0 and 1, drawn as a percent on a 0-100% scale.
SHARE_METRICS = ERROR_RATE_METRICS | {
    "boundary_accuracy",
    "label_accuracy",
    "grouping_score",
    "list_score",
    "auroc",
    "calibration_error",
    "alert_rate",
    "errors_caught",
    "errors_caught_30",
    "verdict_agreement",
    "cached_share",
}
#: Metrics where a lower value is better; their axis title says so with an arrow.
LOWER_IS_BETTER = frozenset(
    {"calibration_error", "over_splits", "merges", "wrong_passes", "wrong_fails"}
)
#: Ratio metrics: a multiple of a reference (1.0 is "as good as random review"),
#: written with one decimal and "×" ("2.5×") on ticks, labels and in tables.
RATIO_METRICS = frozenset({"ecarb_30"})

#: Chart chrome: the takeaway title, the subtitle and the family legend above panels.
TITLE_H = 26
SUB_H = 16
LEGEND_H = 22
#: A panel: its dataset line, an optional note line, the y-axis title, the plot.
SC_LEFT = 46
SC_RIGHT_PAD = 22
SC_PLOT_H = 250
SC_PLOT_H_WIDE = 270
SC_BOTTOM = 46
PANEL_HEAD_H = 20
PANEL_NOTE_H = 15
Y_TITLE_H = 18
COL_GAP = 28
ROW_GAP = 18
#: Width of the broken-axis mark at the left of a cost axis that does not start at $0.
BREAK_W = 14
#: Points whose centres are closer than this, in pixels, (nearly) touch and
#: share one label ("A, B").
STACK_GAP = 14
#: A cost axis starts at $0 unless that would leave more than this share of it empty
#: to the left of the points.
MIN_X_EMPTY = 0.5
#: Average advance of a 12px system-ui point label, regular and semibold, measured in
#: the browser with a little to spare, so a label's box is close to its ink.
LABEL_CHAR_W = 6.0
LABEL_CHAR_W_BOLD = 6.5
#: The same for an 11px label on a line (cost of error, a baseline).
SMALL_CHAR_W = 5.6


def times(v: float, decimals: int = 1) -> str:
    """A ratio as a multiple: 2.4823 to "2.5×"."""
    return f"{v:.{decimals}f}×"


def pct(v: float, decimals: int = 0) -> str:
    """A share as a percent: 0.813 to "81%"."""
    return f"{v * 100:.{decimals}f}%"


def round_step(span: float, max_ticks: int) -> float:
    """The smallest 1, 2 or 5 x 10^k step that covers span in at most max_ticks ticks."""
    raw = span / max(1, max_ticks - 1) if span > 0 else 1.0
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw * (1 - 1e-9))


def _ticks(start: float, end: float, step: float) -> list[float]:
    count = int(round((end - start) / step))
    return [round(start + i * step, 10) for i in range(count + 1)]


def cost_ticks(
    xs: list[float], max_ticks: int = 6, *, from_zero: bool = True
) -> tuple[list[float], bool]:
    """Round ticks for a cost or count axis, and whether the axis is broken.

    A cost axis always starts at 0, so a $4 spread on $135 reads as the same cost.
    With from_zero=False it starts at 0 unless that leaves more than MIN_X_EMPTY of
    its width empty left of the points; it then starts at a round value below the smallest point, and the
    second value returned is True so the chart can draw a broken-axis mark.
    """
    lo, hi = min(xs), max(xs)
    hi = hi if hi > 0 else 1.0
    step = round_step(hi * 1.04, max_ticks)
    top = math.ceil(hi * 1.04 / step) * step
    if not from_zero and lo > 0 and lo / top > MIN_X_EMPTY:
        pad = max((hi - lo) * 0.25, hi * 0.01)
        a, b = lo - pad, hi + pad
        step = round_step(b - a, max_ticks)
        start = math.floor(a / step) * step
        if start > 0:
            return _ticks(start, math.ceil(b / step) * step, step), True
    return _ticks(0.0, top, step), False


def x_axes_differ(xs_by_panel: list[list[float]]) -> bool:
    """Whether a chart's panels have different x axes (each panel has its own)."""
    axes = {tuple(cost_ticks(xs)[0]) for xs in xs_by_panel if xs}
    return len(axes) > 1


def share_ticks(values: list[float]) -> list[float]:
    """Ticks every 10% (or 20% on a long range) from a round value below the data to
    100%, so every chart of a share reads on the same scale."""
    lo = max(0.0, min(values))
    hi = max(values)
    bottom = max(0.0, math.floor((lo - 0.005) * 10) / 10)
    step = 0.1 if 1 - bottom <= 0.5 else 0.2
    if step == 0.2:
        bottom = math.floor(bottom * 5 + 1e-9) / 5
    top = 1.0 if hi <= 1.05 else math.ceil(hi * 10 - 1e-9) / 10
    return _ticks(bottom, top, step)


def _money_ticks(ticks: list[float]) -> list[str]:
    """Whole dollars, or cents when a tick falls between them."""
    whole = all(abs(t - round(t)) < 1e-9 for t in ticks)
    return [f"${t:,.0f}" if whole else f"${t:,.2f}" for t in ticks]


def _compact(v: float, axis_max: float) -> str:
    """One style per axis: compact (5K, 10K) when the axis reaches 10,000."""
    if axis_max >= 10_000:
        return f"{v / 1000:,.{0 if v % 1000 == 0 else 1}f}K" if v else "0"
    return f"{v:,.0f}"


def _frontier(points: list[tuple[str, dict[str, float], dict[str, float]]]) -> set[str]:
    """Values no other value beats on both axes (cheaper and more accurate)."""
    keep = set()
    for v, x, y in points:
        if not any(
            ox["mean"] <= x["mean"]
            and oy["mean"] >= y["mean"]
            and (ox["mean"] < x["mean"] or oy["mean"] > y["mean"])
            for ov, ox, oy in points
            if ov != v
        ):
            keep.add(v)
    return keep


Rect = tuple[float, float, float, float]


def _rect(lx: float, ly: float, w: float, anchor: str) -> Rect:
    """The box a one-line label at (lx, ly) with this anchor covers."""
    left = lx if anchor == "start" else lx - w if anchor == "end" else lx - w / 2
    return (left, ly - 11, left + w, ly + 3)


def _hits(r: Rect, others: list[Rect]) -> bool:
    return any(
        r[0] < o[2] and o[0] < r[2] and r[1] < o[3] and o[1] < r[3] for o in others
    )


def _clear(r: Rect, bounds: Rect, blocked: list[Rect]) -> bool:
    x0, y0, x1, y1 = bounds
    if r[0] < x0 or r[2] > x1 or r[1] < y0 or r[3] > y1:
        return False
    return not _hits(r, blocked)


def _segment_boxes(ax: float, ay: float, bx: float, by: float) -> list[Rect]:
    """Small boxes along a segment, so a label can be kept off a line."""
    n = max(2, int(math.hypot(bx - ax, by - ay) / 4))
    return [
        (ax + t * (bx - ax) - 1, ay + t * (by - ay) - 1)
        + (ax + t * (bx - ax) + 1, ay + t * (by - ay) + 1)
        for t in (i / n for i in range(1, n))
    ]


def _leader_end(cx: float, cy: float, r: Rect) -> tuple[float, float]:
    """The point of a label's box nearest its dot, where a leader line ends."""
    return (min(max(cx, r[0] - 2), r[2] + 2), min(max(cy, r[1] - 1), r[3] + 1))


#: Spots right beside a dot, tried first: right, left, above, below, diagonals.
NEAR = (
    (10, 4, "start"),
    (-10, 4, "end"),
    (0, -11, "middle"),
    (0, 19, "middle"),
    (8, -9, "start"),
    (8, 17, "start"),
    (-8, -9, "end"),
    (-8, 17, "end"),
)
#: A label beside a dot must keep this far from every other dot, so it is plainly that
#: dot's label and not a neighbor's.
OWN_GAP = 9
#: Above and below, a label beside its dot keeps further from the others: a label a
#: line above or below another dot reads as that dot's.
OWN_GAP_Y = 14
#: How far from its mark a label at the end of the mark's own error bar may sit.
OWN_BAR_REACH = 26
#: Room kept between two labels, so two names never run into one.
LABEL_GAP = 5
#: The longest leader line a label may have, from its dot's edge, in pixels. A point
#: whose label needs a longer one is numbered instead, and only it goes in the key.
MAX_LEADER = 60

Label = tuple[str, float, float, str, tuple[float, float] | None]
#: A point to label: (key, text, x, y, character width).
LabelPoint = tuple[str, str, float, float, float]


def _inflate(r: Rect, by: float, by_y: float | None = None) -> Rect:
    dy = by if by_y is None else by_y
    return (r[0] - by, r[1] - dy, r[2] + by, r[3] + dy)


def _fit(cx: float, w: float, x0: float, x1: float) -> tuple[float, str]:
    """A label centred over cx, or anchored to the plot edge it would cross, so its
    text ends over its point rather than drifting toward a neighbour's."""
    if cx + w / 2 > x1:
        return x1, "end"
    if cx - w / 2 < x0:
        return x0, "start"
    return cx, "middle"


def _candidates(
    cx: float, cy: float, w: float, x0: float, x1: float
) -> list[tuple[float, float, str]]:
    """Spots a little further from a dot than NEAR, nearest first, for a short leader."""
    out = []
    for dx in (10, 20, 30, 45, 60):
        for dy in (4, -8, 16, -20, 28, -32, 40, -44, 52):
            out += [(cx + dx, cy + dy, "start"), (cx - dx, cy + dy, "end")]
    for dy in (-16, 22, -26, 32, -36, 42, -48, 54, -60, 66):
        lx, anchor = _fit(cx, w, x0, x1)
        out += [
            (lx, cy + dy, anchor),
            (cx + 4, cy + dy, "start"),
            (cx - 4, cy + dy, "end"),
        ]
    return out


def _place_labels(
    points: list[LabelPoint],
    bounds: Rect,
    fixed: list[Rect],
    boxes: dict[str, list[Rect]],
    *,
    strict: bool = True,
    crowded: list[str] | None = None,
) -> tuple[dict[str, Label], list[str]]:
    """Place one label per point, clear of every other label, dot, error bar and line.

    points: (key, text, x, y, character width); fixed: boxes no label or leader may
    touch (a reference line, the axis title); boxes: each point's dot and error bar.
    A label first tries the spots right beside its point (NEAR), keeping clear of the
    other dots so it reads as its own point's; then a spot a little further out with a
    leader of at most MAX_LEADER pixels that passes nothing. Returns ({key: (text, x,
    y, anchor, leader end or None)}, keys that found no spot); not strict, such a
    label takes the spot above its dot instead of failing.
    """
    x0, _, x1, _ = bounds
    every = [b for bs in boxes.values() for b in bs]
    dots = {key: (cx - 7, cy - 7, cx + 7, cy + 7) for key, _, cx, cy, _ in points}
    centres = {key: (cx, cy) for key, _, cx, cy, _ in points}
    taken: list[Rect] = []
    out: dict[str, Label] = {}
    pending: list[LabelPoint] = []
    failed: list[str] = []
    for key, text, cx, cy, cw in sorted(points, key=lambda p: (p[3], p[2])):
        w = len(text) * cw
        others = [_inflate(d, OWN_GAP, OWN_GAP_Y) for k, d in dots.items() if k != key]
        for dx, dy, anchor in NEAR:
            r = _rect(cx + dx, cy + dy, w, anchor)
            if _clear(r, bounds, taken + others + fixed + every) and _owns(
                r, key, centres
            ):
                taken.append(_inflate(r, LABEL_GAP, 1))
                out[key] = (text, cx + dx, cy + dy, anchor, None)
                break
        else:
            pending.append((key, text, cx, cy, cw))
    for key, text, cx, cy, cw in pending:
        w = len(text) * cw
        others = [_inflate(d, OWN_GAP) for k, d in dots.items() if k != key]
        # A leader passes no other point's mark or bar, nor its own bar: a leader
        # drawn along an error bar reads as part of it.
        passed = (
            fixed
            + taken
            + [b for k, bs in boxes.items() if k != key for b in bs]
            + boxes.get(key, [])[1:]
        )
        spot = None
        # Just past the end of the point's own error bar, centred on it, the label
        # reads as the point's with no leader at all.
        own_bar = boxes.get(key, [])[1:]
        # Such a label must not also sit over a neighbour's point or bar, or it reads
        # as the neighbour's.
        near_bars = [
            (b[0] - 4, b[1] - 40, b[2] + 4, b[3] + 40)
            for k, bs in boxes.items()
            if k != key
            for b in bs
        ]
        for _, top, _, bottom in own_bar:
            lx, anchor = _fit(cx, w, x0, x1)
            for ly in (top - 4, bottom + 13):
                r = _rect(lx, ly, w, anchor)
                # Only where the bar is short: past the end of a long one the label
                # floats far from its mark and reads as nobody's.
                if (
                    abs(ly - cy) <= OWN_BAR_REACH
                    and spot is None
                    and _clear(r, bounds, taken + others + fixed + every + near_bars)
                    and _owns(r, key, centres)
                ):
                    spot = (text, lx, ly, anchor, None)
        for lx, ly, anchor in [] if spot else _candidates(cx, cy, w, x0, x1):
            r = _rect(lx, ly, w, anchor)
            if not _clear(r, bounds, taken + others + fixed + every):
                continue
            if not _owns(r, key, centres):
                continue
            end = _leader_end(cx, cy, r)
            if math.hypot(end[0] - cx, end[1] - cy) - 7 > MAX_LEADER:
                continue
            if _hits_any(_segment_boxes(cx, cy, *end)[2:], passed):
                continue
            spot = (text, lx, ly, anchor, end)
            break
        if spot is None and not strict:
            # Further out, still clear of everything, with a longer leader that
            # passes no other mark: better than a label over another's.
            for lx, ly, anchor in _far_candidates(cx, cy, w, x0, x1):
                r = _rect(lx, ly, w, anchor)
                if not _clear(r, bounds, taken + others + fixed + every):
                    continue
                end = _leader_end(cx, cy, r)
                # A leader says whose label it is: its end must be nearest its mark.
                if not _owns_end(end, key, centres):
                    continue
                if math.hypot(end[0] - cx, end[1] - cy) - 7 > MAX_LEADER_FAR:
                    continue
                if _hits_any(_segment_boxes(cx, cy, *end)[2:], passed):
                    continue
                spot = (text, lx, ly, anchor, end)
                break
        if spot is None:
            if strict:
                failed.append(key)
                continue
            if crowded is not None:
                crowded.append(key)
            spot = _least_crowded(
                text,
                (key, centres),
                w,
                bounds,
                taken,
                others + fixed + every,
                passed,
            )
        taken.append(_inflate(_rect(spot[1], spot[2], w, spot[3]), LABEL_GAP, 1))
        out[key] = spot
    return out, failed


#: The longest leader a label of a crowded panel may take before it overlaps anything.
MAX_LEADER_FAR = 150


def _far_candidates(
    cx: float, cy: float, w: float, x0: float, x1: float
) -> list[tuple[float, float, str]]:
    """Spots out to MAX_LEADER_FAR from a dot, nearest first."""
    out = []
    for dy in range(-150, 151, 6):
        for dx in (0, 12, 30, 55, 85, 120):
            if dx == 0:
                lx, anchor = _fit(cx, w, x0, x1)
                out.append((lx, cy + dy, anchor))
            else:
                out += [(cx + dx, cy + dy, "start"), (cx - dx, cy + dy, "end")]
    return sorted(out, key=lambda s: math.hypot(s[0] - cx, s[1] - cy))


def _check_owned(
    placed: dict[str, Label], centres: dict[str, tuple[float, float]], char_w: Any
) -> list[str]:
    """The labels nearer another mark than their own, marked "?": a label beside
    the wrong point says the wrong thing about the data, so draw_scatter refuses to
    draw a chart that still has one."""
    bad: list[str] = []
    for key, (text, lx, ly, anchor, end) in placed.items():
        if end is None:
            ok = _owns(_rect(lx, ly, len(text) * char_w(key), anchor), key, centres)
        else:
            ok = _owns_end(end, key, centres)
        if not ok:
            bad.append(f"?{text}")
    return bad


def _least_crowded(
    text: str,
    owner: tuple[str, dict[str, tuple[float, float]]],
    w: float,
    bounds: Rect,
    labels: list[Rect],
    blocked: list[Rect],
    passed: list[Rect],
) -> Label:
    """The spot, inside the plot, whose label overlaps the least, nearest first: for
    a label of a small panel that has no clear spot at all. Overlapping another label
    costs most, then a leader across a mark or bar, then touching a mark. A spot
    nearer another point than this one is taken only when there is no other."""
    key, centres = owner
    cx, cy = centres[key]
    own_dot = (cx - 6, cy - 6, cx + 6, cy + 6)
    x0, _, x1, _ = bounds
    spots = [(cx + dx, cy + dy, a) for dx, dy, a in NEAR] + _candidates(
        cx, cy, w, x0, x1
    )
    for dy in range(-120, 121, 12):
        lx, anchor = _fit(cx, w, x0, x1)
        spots.append((lx, cy + dy, anchor))
        for dx in (10, 30, 60, 90):
            spots += [(cx + dx, cy + dy, "start"), (cx - dx, cy + dy, "end")]
    spots += _far_candidates(cx, cy, w, x0, x1)
    best: tuple[float, float, Label] | None = None
    for lx, ly, anchor in spots:
        r = _rect(lx, ly, w, anchor)
        if r[0] < x0 or r[2] > x1 or r[1] < bounds[1] or r[3] > bounds[3]:
            continue
        end = _leader_end(cx, cy, r)
        far = math.hypot(end[0] - cx, end[1] - cy)
        # Never nearer another mark than its own, and never over its own mark.
        owned = _owns_end(end, key, centres) if far > 9 else _owns(r, key, centres)
        if not owned or _hits(r, [own_dot]):
            continue
        hits = 50 * sum(_hits(r, [b]) for b in labels)
        hits += sum(_hits(r, [b]) for b in blocked)
        if far > 9:
            path = _segment_boxes(cx, cy, *end)[2:]
            hits += 3 * sum(_hits_any(path, [b]) for b in passed)
        if best is None or (hits, far) < best[:2]:
            best = (hits, far, (text, lx, ly, anchor, end if far > 9 else None))
    if best is None:
        lx, anchor = _fit(cx, w, x0, x1)
        return (text, lx, cy - 12, anchor, None)
    return best[2]


def _gap(r: Rect, x: float, y: float) -> float:
    """How far (x, y) is from the nearest edge of r; 0 inside it."""
    return math.hypot(max(r[0] - x, 0, x - r[2]), max(r[1] - y, 0, y - r[3]))


def _owns_end(
    end: tuple[float, float], key: str, centres: dict[str, tuple[float, float]]
) -> bool:
    """Whether a leader's end, at its label, is nearer its own point than any other."""
    own = math.hypot(end[0] - centres[key][0], end[1] - centres[key][1])
    return all(
        own < math.hypot(end[0] - c[0], end[1] - c[1])
        for k, c in centres.items()
        if k != key
    )


def _owns(r: Rect, key: str, centres: dict[str, tuple[float, float]]) -> bool:
    """Whether a label's box is nearer its own point than any other point, so it
    cannot be read as another point's label."""
    own = _gap(r, *centres[key])
    return all(own < _gap(r, *c) for k, c in centres.items() if k != key)


def _hits_any(boxes: list[Rect], others: list[Rect]) -> bool:
    return any(_hits(b, others) for b in boxes)


#: Model families, in their fixed color order. A value whose label names none of them is
#: in "other". Each family has a color (STYLE) and a marker shape, so a reader who
#: cannot tell the colors apart still can tell the families apart.
FAMILIES = (("nova", "Amazon Nova"), ("claude", "Claude"))
FAMILY_ORDER = ("nova", "claude", "other")
SHAPES = {"nova": "circle", "claude": "square", "other": "diamond"}

#: A scatter point: (value, x {"mean", "se"}, {metric key: y {"mean", "se"}}).
Point = tuple[str, dict[str, float], dict[str, dict[str, float]]]
#: A dataset's panel: (header, note, points, {metric key: (baseline y, name)}). The
#: header names the dataset and its sample; the note, when not "", is a second line.
Group = tuple[str, str, list[Point], dict[str, tuple[float, str]]]
#: A plotted metric: (key, name, axis title, drawn as a percent).
YMetric = tuple[str, str, str, bool]


def family(value: str) -> str:
    """The model family a value's label names, or "other"."""
    for key, name in FAMILIES:
        if name in value:
            return key
    return "other"


def family_title(fam: str, values: list[str]) -> str:
    """A family's name; for "other", the vendors its values name ("OpenAI and xAI")."""
    for key, name in FAMILIES:
        if key == fam:
            return name
    vendors: list[str] = []
    for value in values:
        words = value.split(" · ")[-1].split()
        if words and words[0] not in vendors:
            vendors.append(words[0])
    return " and ".join(vendors) or "Other models"


def marker(shape: str, cx: float, cy: float, r: float, cls: str) -> str:
    """A dot, square or diamond centred on (cx, cy) with about the same area."""
    if shape == "square":
        s = r * 0.9
        return (
            f'<rect class="{cls}" x="{cx - s:.1f}" y="{cy - s:.1f}" '
            f'width="{2 * s:.1f}" height="{2 * s:.1f}" rx="1.5"/>'
        )
    if shape == "diamond":
        d = r * 1.25
        return (
            f'<path class="{cls}" d="M{cx:.1f} {cy - d:.1f}L{cx + d:.1f} {cy:.1f}'
            f'L{cx:.1f} {cy + d:.1f}L{cx - d:.1f} {cy:.1f}Z"/>'
        )
    return f'<circle class="{cls}" cx="{cx:.1f}" cy="{cy:.1f}" r="{r}"/>'


def _y_ticks(ys: list[float], means: list[float]) -> list[float]:
    """Ticks covering ys, stopping at 1.0 when no mean passes it (a score cannot)."""
    yt = _span(ys, allow_negative=min(ys) < 0)
    if max(means) <= 1.0:
        yt = [t for t in yt if t <= 1.0 + 1e-9]
        if yt[-1] < 1.0 and max(ys) > yt[-1]:
            yt.append(1.0)
    return yt


def _span(
    values: list[float],
    *,
    min_span: float = 0.0,
    pad: float = 0.12,
    count: int = 4,
    allow_negative: bool = True,
) -> list[float]:
    """Return round ticks covering the values, padded, with a minimum span."""
    lo, hi = min(values), max(values)
    if min_span and hi - lo < min_span * abs(hi):
        grow = (min_span * abs(hi) - (hi - lo)) / 2
        lo, hi = lo - grow, hi + grow
    margin = (hi - lo) * pad or abs(hi) * 0.05 or 1.0
    lo, hi = lo - margin, hi + margin
    if not allow_negative:
        lo = max(0.0, lo)
    return nice_ticks(lo, hi, count)


def heading_svg(title: str, subtitle: str, top: float = 0) -> tuple[str, float]:
    """The chart's takeaway title and its one muted subtitle line (wrapped if long)."""
    out = [
        f'<text class="ctitle" x="0" y="{top + 18:.1f}">{escape(title)}</text>'
        if title
        else ""
    ]
    y = top + (TITLE_H if title else 0)
    lines = wrap_subtitle(subtitle, WIDTH * SUB_CHAR_W / SUB12_CHAR_W)
    for line in lines:
        out.append(f'<text class="sub" x="0" y="{y + 12:.1f}">{escape(line)}</text>')
        y += SUB_H
    return "".join(out), y - top


#: Average advance of the 12px subtitle, to wrap it.
SUB12_CHAR_W = 6.1


def family_legend(fams: list[str], values: list[str], y: float) -> str:
    """A marker and a name per family present, in the fixed family order."""
    out, x = [], 0.0
    for fam in fams:
        name = family_title(fam, [v for v in values if family(v) == fam])
        out.append(marker(SHAPES[fam], x + 6, y - 4, 5, f"dot fam-{fam}"))
        out.append(
            f'<text class="label" x="{x + 16:.1f}" y="{y:.1f}">{escape(name)}</text>'
        )
        x += 16 + len(name) * CHAR_W + 22
    return "".join(out)


def _scatter_panel(
    *,
    width: float,
    plot_h: float,
    head_h: float,
    header: str,
    note: str,
    xt: list[float],
    x_ticks: list[str],
    broken: bool,
    x_title: str,
    yt: list[float],
    y_title: str,
    percent: bool,
    y_name: str,
    points: list[tuple[str, dict[str, float], dict[str, float]]],
    labels: dict[str, str],
    default: str | None,
    baseline: tuple[float, str] | None,
    failures: list[str],
    colored: bool,
    x_exact: Any,
    tips: dict[str, str] | None = None,
    ratio: bool = False,
) -> str:
    """One scatter panel in its own coordinates.

    points: (value, x, y); default: the reference point, drawn and labelled bold;
    failures: the chart's points whose name found no clear spot, shared by its
    panels; such a name takes the least crowded spot near its point, and the chart
    can split into one panel per model instead. Nothing is ever numbered.
    ratio: y is a multiple (RATIO_METRICS), written "2.5×".
    """
    left, right = SC_LEFT, width - SC_RIGHT_PAD
    y0 = head_h + Y_TITLE_H + 6
    y1 = y0 + plot_h
    x_from = left + (BREAK_W if broken else 0)

    def px(v: float) -> float:
        return x_from + (v - xt[0]) / (xt[-1] - xt[0]) * (right - x_from)

    def py(v: float) -> float:
        return y1 - (v - yt[0]) / (yt[-1] - yt[0]) * (y1 - y0)

    def y_text(v: float) -> str:
        return pct(v) if percent else times(v) if ratio else fmt(v)

    body: list[str] = []
    if header:
        body.append(f'<text class="ptitle" x="0" y="14">{escape(header)}</text>')
    if note:
        body.append(
            f'<text class="sub" x="0" y="{PANEL_HEAD_H + 11}">{escape(note)}</text>'
        )
    if y_title:
        body.append(
            f'<text class="axislabel" x="{left - 4}" y="{head_h + 13}">'
            f"{escape(y_title)}</text>"
        )
    y_dec = tick_decimals(yt)
    for t in yt:
        if ratio:
            text = times(t, max(1, y_dec))
        else:
            text = pct(t) if percent else f"{t:,.{y_dec}f}"
        body.append(
            f'<line class="grid" x1="{left}" x2="{right}" '
            f'y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
            f'<line class="axis" x1="{left - 4}" x2="{left}" '
            f'y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
            f'<text class="tick" x="{left - 7}" y="{py(t) + 4:.1f}" '
            f'text-anchor="end">{text}</text>'
        )
    for t, text in zip(xt, x_ticks, strict=True):
        body.append(
            f'<line class="axis" x1="{px(t):.1f}" x2="{px(t):.1f}" '
            f'y1="{y1}" y2="{y1 + 4}"/>'
            f'<text class="tick" x="{px(t):.1f}" y="{y1 + 16}" '
            f'text-anchor="middle">{text}</text>'
        )
    body.append(
        f'<line class="axis" x1="{left}" x2="{right}" y1="{y1}" y2="{y1}"/>'
        f'<line class="axis" x1="{left}" x2="{left}" y1="{y0}" y2="{y1}"/>'
    )
    if x_title:
        body.append(
            f'<text class="axislabel" x="{(left + right) / 2:.1f}" '
            f'y="{y1 + 36}" text-anchor="middle">{escape(x_title)}</text>'
        )
    if broken:
        # Two slanted strokes across a gap in the axis: the axis does not start at 0.
        mx = left + BREAK_W / 2
        body.append(
            f'<rect class="gap" x="{mx - 4:.1f}" y="{y1 - 6}" width="8" height="12"/>'
            f'<line class="axis" x1="{mx - 5:.1f}" y1="{y1 + 4}" '
            f'x2="{mx - 1:.1f}" y2="{y1 - 4}"/>'
            f'<line class="axis" x1="{mx + 1:.1f}" y1="{y1 + 4}" '
            f'x2="{mx + 5:.1f}" y2="{y1 - 4}"/>'
        )
    fixed: list[Rect] = [(left - 4, head_h, left + len(y_title) * 6.2, head_h + 18)]
    tail: list[str] = []
    if baseline is not None:
        value, name = baseline
        by = py(value)
        w = len(name) * SMALL_CHAR_W
        body.append(
            f"<g><title>{escape(name)}: {escape(y_text(value))}</title>"
            f'<line class="baseline" x1="{left}" x2="{right}" '
            f'y1="{by:.1f}" y2="{by:.1f}"/></g>'
        )
        # The line itself, and its own label at the right end, keep every point
        # label off them.
        fixed.append((left, by - 3, right, by + 3))
        fixed.append(_rect(right - 4, by - 4, w, "end"))
        tail.append(
            f'<text class="baselinelabel halo" x="{right - 4:.1f}" y="{by - 4:.1f}" '
            f'text-anchor="end">{escape(name[:1].upper() + name[1:])}</text>'
        )
    centre = {v: (px(x["mean"]), py(y["mean"])) for v, x, y in points}
    boxes: dict[str, list[Rect]] = {}
    for value, _, y in points:
        cx, cy = centre[value]
        dy = 2 * y.get("se", 0)
        band_top = max(y0, py(y["mean"] + dy))
        band_bottom = min(y1, py(y["mean"] - dy))
        boxes[value] = [(cx - 7, cy - 7, cx + 7, cy + 7)]
        if band_bottom > band_top:
            boxes[value].append((cx - BAND - 1, band_top, cx + BAND + 1, band_bottom))
            cls = f"band fam-{family(value)}" if colored else "band"
            body.append(
                f'<rect class="{cls}" x="{cx - BAND:.1f}" y="{band_top:.1f}" '
                f'width="{2 * BAND}" height="{band_bottom - band_top:.1f}" '
                f'rx="{BAND}"/>'
            )
    bounds = (left + 2, y0 - 8, right, y1 - 2)
    # Points drawn on top of each other share one label ("Off, Separate"), on the
    # first of them, rather than one name hiding the other.
    stacked: dict[str, list[str]] = {}
    for v, _, _ in points:
        cx, cy = centre[v]
        lead = next(
            (
                k
                for k in stacked
                if math.hypot(centre[k][0] - cx, centre[k][1] - cy) < STACK_GAP
            ),
            None,
        )
        stacked.setdefault(lead or v, []).append(v)

    def text_of(lead: str, short: bool) -> str:
        parts = []
        for v in stacked[lead]:
            t = labels.get(v, v)
            if short and v != default:
                t = re.sub(r"\s*\([^()]*\)", "", t).strip() or t
            parts.append(t)
        return ", ".join(parts)

    def bold(lead: str) -> bool:
        return default in stacked[lead]

    def attempt(
        texts: dict[str, str], strict: bool, crowded: list[str] | None = None
    ) -> tuple[dict[str, Label], list[str]]:
        return _place_labels(
            [
                (
                    k,
                    texts[k],
                    *centre[k],
                    LABEL_CHAR_W_BOLD if bold(k) else LABEL_CHAR_W,
                )
                for k in stacked
            ],
            bounds,
            fixed,
            boxes,
            strict=strict,
            crowded=crowded,
        )

    texts = {k: text_of(k, False) for k in stacked}
    placed, failed = attempt(texts, True)
    if failed:
        # Shorter names first: a value's note in parentheses goes, the reference's
        # "(recommended)" stays.
        texts = {k: text_of(k, True) for k in stacked}
        placed, failed = attempt(texts, True)

    def trimmable(k: str) -> bool:
        return ", " in texts[k].partition(" · ")[2]

    while failed and any(trimmable(k) for k in failed):
        # A setup listed after " · " loses its last part until it fits.
        for k in failed:
            if trimmable(k):
                head, sep, setup = texts[k].partition(" · ")
                texts[k] = f"{head}{sep}{setup.rsplit(', ', 1)[0]}"
        placed, failed = attempt(texts, True)
    if failed:
        failures.extend(failed)
        # A name that found no clear spot at all is marked "!": its chart is worth
        # redrawing with more room.
        crowded: list[str] = []
        placed, _ = attempt(texts, False, crowded)
        failures.extend(f"!{k}" for k in crowded)
    failures += _check_owned(
        placed,
        {k: centre[k] for k in stacked},
        lambda k: LABEL_CHAR_W_BOLD if bold(k) else LABEL_CHAR_W,
    )
    for value, (_, _, _, _, end) in placed.items():
        if end is not None:
            cx, cy = centre[value]
            d = math.hypot(end[0] - cx, end[1] - cy) or 1
            sx, sy = cx + (end[0] - cx) / d * 7, cy + (end[1] - cy) / d * 7
            body.append(
                f'<line class="leader" x1="{sx:.1f}" y1="{sy:.1f}" '
                f'x2="{end[0]:.1f}" y2="{end[1]:.1f}"/>'
            )
    for value, x, y in points:
        cx, cy = centre[value]
        fam = family(value)
        cls = f"dot fam-{fam}" if colored else "dot"
        if value == default:
            cls += " ref"
        body.append(
            f"<g><title>{escape((tips or labels).get(value, value))}: {escape(y_name)} "
            f"{escape(y_text(y['mean']))} ± {escape(y_text(2 * y.get('se', 0)))}, "
            f"{escape(x_exact(x['mean']))}</title>"
            + marker(SHAPES[fam] if colored else "circle", cx, cy, 5, cls)
            + "</g>"
        )
    for value, (text, lx, ly, anchor, _) in placed.items():
        cls = "plabel dflt" if bold(value) else "plabel"
        inner = escape(text)
        if len(stacked[value]) > 1 and bold(value):
            # One name of a shared label is the reference: only it is bold.
            cls = "plabel"
            parts = (
                text.split(", ") if text.count(", ") == len(stacked[value]) - 1 else []
            )
            inner = ", ".join(
                f'<tspan class="dflt">{escape(t)}</tspan>'
                if v == default
                else escape(t)
                for v, t in zip(stacked[value], parts, strict=False)
            ) or escape(text)
        body.append(
            f'<text class="{cls} halo" x="{lx:.1f}" y="{ly:.1f}" '
            f'text-anchor="{anchor}">{inner}</text>'
        )
    return "".join(body + tail)


def lower_name(name: str) -> str:
    """Lower-case a metric name mid-sentence, leaving an acronym such as AUROC alone."""
    return name if name[:2].isupper() else name[:1].lower() + name[1:]


#: A sample size as a chart names it: "20 × 3 runs" to "20 documents × 3 runs".
N_RUNS = re.compile(r"\b(\d[\d,]*(?:–\d[\d,]*)?) × (\d+(?:–\d+)?) runs?\b")
#: A subtitle's "Fixed: ..." part, which a chart starting from a baseline replaces.
FIXED_PART = re.compile(r"\s*·\s*Fixed: [^·]*")
FOOT_H = 14


def _with_unit(text: str, unit: str) -> str:
    return N_RUNS.sub(lambda m: f"{m.group(1)} {unit} × {m.group(2)} runs", text)


def chart_labels(
    values: list[str],
    labels: dict[str, str],
    default: str | None,
    ab: dict[str, Any] | None,
    reference: str | None = None,
    titled: bool = False,
) -> tuple[dict[str, str], str | None]:
    """Each value's point label, and the reference point drawn bold.

    With a `reference` (the registry's, or passed in) that point is the bold one and
    is named by its value, marked "(recommended)" when the value does not say what
    it is already; the "(default)" value counts for nothing. Without one, the default
    value is the reference, as before. A reference point whose label names no model
    while other points do, or whose model no label names, gets its extraction model
    from the results ("Default · Sonnet 5"), so no point leaves its model to guess,
    unless the chart's title names the one model every point ran (`titled`).
    """
    out = {v: labels.get(v, v) for v in values}
    reference = reference or (ab or {}).get("reference")
    if (
        reference in out
        and default in out
        and default != reference
        and ab
        and not _names_model(out[default])
        and any(_names_model(t) for v, t in out.items() if v != default)
    ):
        # The default names no model while the others do: name the ones it ran.
        setup = _setup_text(str(ab["id"]), default)
        model = _extraction_model(str(ab["id"]), default)
        if setup and out[default].lower() in ("default", "preset", "preset default"):
            out[default] = f"Preset default · {setup}"
        elif model:
            out[default] = f"{out[default]} · {model}"
    if reference and reference in out:
        text = out[reference]
        if "(" not in text:
            word = "baseline setup" if (ab or {}).get("baseline") else "recommended"
            text = f"{text} ({word})"
        out[reference] = text
        return out, reference
    if default in out and ab and not titled and not _names_model(out[default]):
        model = _extraction_model(str(ab["id"]), default)
        if model:
            text = (
                "Preset default"
                if out[default].lower() in ("default", "preset")
                else out[default]
            )
            out[default] = f"{text} · {model}"
    return out, default


#: The words for an `extraction.mode` every value sets.
MODE_WORDS = {"advanced": "agentic", "simple": "single-pass"}


def _exceptions(
    ab: dict[str, Any] | None, base: dict[str, Any], held: str, held_name: str
) -> str:
    """ ", except agentic extraction on Claude Sonnet 5.5": what every value of the
    ablation sets that the baseline does not, for its footnote."""
    base_set = base.get("set") or {}
    mode = _held_model(ab, "extraction.mode")
    mode_word = (
        MODE_WORDS.get(mode, mode) if mode != base_set.get("extraction.mode") else ""
    )
    model = held_name if held and held != base_set.get("extraction.model") else ""
    if not mode_word and not model:
        return ""
    words = f"{mode_word} extraction" if mode_word else "extraction"
    return f", except {words}" + (f" on {model}" if model else "")


def _setup_text(ablation_id: str, value: str) -> str:
    """ "Sonnet 5, Textract tables, Nova Lite scoring": a value's extraction model,
    OCR features and scoring model, from its results, or ""."""
    found = [
        m
        for k, m in _results_models(ablation_id).items()
        if k.split("|")[1:2] == [value]
    ]
    if not found:
        return ""
    m = found[0]

    def bare(text: str) -> str:
        return VENDOR.sub("", str(text).split(",")[0]).strip()

    parts = [bare(m.get("Extraction") or "")]
    ocr = str(m.get("OCR") or "")
    if "Tables" in ocr:
        parts.append("Textract tables")
    elif "(Layout)" in ocr:
        parts.append("Layout only")
    conf = str(m.get("Confidence") or "")
    if conf:
        parts.append("no scoring" if conf == "Off" else f"{bare(conf)} scoring")
    return ", ".join(p for p in parts if p)


def _chart_texts(
    subtitle: str,
    groups: list[Group],
    ab: dict[str, Any] | None,
    unit: str,
    title: str = "",
) -> tuple[str, list[str], str]:
    """The subtitle, each panel's note, and a footnote naming the baseline.

    A chart whose values start from a baseline says so in its subtitle ("Everything
    else: recommended setup") instead of naming one held model, and its footnote
    spells the baseline out, models and all. Sample sizes name their unit.
    """
    base = _baseline(ab)
    notes = [g[1] for g in groups]
    foot = ""
    held = _held_model(ab)
    held_name = _model_name(held) if held else ""
    # What every value holds that the baseline does not: the registry's note, else
    # the extraction model every value sets.
    note = str((ab or {}).get("fixed_note") or "")
    if not note and held_name:
        note = f"Extraction on {held_name}"
    if held_name and held_name in title:
        # The title names the model already: keep only what the note adds to it.
        note = re.sub(rf"^Extraction on {re.escape(held_name)}\s*", "", note)
        note = re.sub(r"^\((.*)\)$", r"\1", note).strip()
    if base:
        name = str(base.get("name") or "the baseline")
        subtitle = FIXED_PART.sub("", subtitle).strip(" ·")
        if note:
            subtitle = f"{subtitle} · {note}"
        subtitle = f"{subtitle} · Everything else: {name[:1].lower()}{name[1:]}"
        notes = ["" if n.startswith("Fixed:") else n for n in notes]
        if base.get("summary"):
            lead = name + _exceptions(ab, base, held, held_name)
            foot = f"{lead}: {str(base['summary']).rstrip('.')}."
    elif ab:
        if note and (not held_name or held_name not in subtitle):
            subtitle = f"{subtitle} · {note}"
        if not ab.get("interim"):
            subtitle = f"{subtitle} · Everything else: each dataset's preset"
    if (ab or {}).get("interim"):
        foot = f"{foot} {ab['interim']}".strip()  # type: ignore[index]
    return _with_unit(subtitle, unit), notes, foot


def _own_y(
    found: list[list[dict[str, float]]], extra: list[list[float]], percent: bool
) -> list[list[float]]:
    """Each panel's y ticks: one range shared by every panel of the metric.

    found: each panel's y cells; extra: each panel's reference-line values.
    """

    def ticks(cells: list[dict[str, float]], more: list[float]) -> list[float]:
        vals = [y["mean"] + s * 2 * y.get("se", 0) for y in cells for s in (-1, 1)]
        vals += more
        if percent:
            return share_ticks(vals)
        return _y_ticks(vals, [y["mean"] for y in cells])

    # Panels of one metric side by side always share it: a range of its own would
    # make a small gap in one panel look as large as a big one in the next.
    every = [y for cells in found for y in cells]
    shared = ticks(every, [v for vs in extra for v in vs])
    return [shared] * len(found)


#: The gap between the per-model panels a crowded chart splits into, and their
#: plot height: taller than a two-up panel, as a narrow panel packs its points closer.
FACET_GAP = 18
FACET_PLOT_H = 290


def _facets(
    values: list[str], labels: dict[str, str], families: bool
) -> tuple[list[str], dict[str, str], dict[str, str]] | None:
    """How a crowded chart splits into one panel per model: (panel names in order,
    each value's panel, each value's label within it), or None when it cannot.

    Values that end in a model ("Holistic · Claude Haiku 5.5") split by that model and
    drop it from their labels, since the panel names it; values that are models split
    by family.
    """
    if all(" · " in v for v in values):
        models = {
            v: re.sub(r"\s*\([^()]*\)", "", v.rpartition(" · ")[2]).strip()
            for v in values
        }
        if len(set(models.values())) > 1 and all(map(_names_model, models.values())):
            within = {}
            for v in values:
                text = labels.get(v, v)
                head, sep, tail = text.rpartition(" · ")
                note = re.search(r"\s*\([^()]*\)\s*$", tail) if sep else None
                within[v] = (head + (note.group(0) if note else "")) if sep else text
            return _facet_order(models, values), models, within
    if families and len({family(v) for v in values}) > 1:
        fams = {v: family(v) for v in values}
        names = {
            f: family_title(f, [v for v in values if fams[v] == f])
            for f in fams.values()
        }
        of = {v: names[fams[v]] for v in values}
        return _facet_order(of, values), of, {v: labels.get(v, v) for v in values}
    return None


def _facet_order(of: dict[str, str], values: list[str]) -> list[str]:
    """Panels in the family order (Nova, Claude, others), then by first appearance."""
    first = {}
    for i, v in enumerate(values):
        first.setdefault(of[v], (FAMILY_ORDER.index(family(v)), i))
    return sorted(first, key=lambda f: first[f])


def draw_scatter(
    x_name: str,
    ys: list[YMetric],
    groups: list[Group],
    runs: int,
    *,
    title: str = "",
    subtitle: str = "",
    labels: dict[str, str] | None = None,
    default: str | None = None,
    x_money: bool = False,
    unit: str = "documents",
    mcoe: frozenset[str] | set[str] = frozenset(),
    prices: str = "",
    families: bool = False,
    reference: str | None = None,
    cost_axis: str = "",
) -> str:
    """Return an SVG document plotting each y metric against x (cost or tokens).

    ys: the metrics on the y-axis, a row of panels each.
    groups: one panel per dataset, [(header, note, points, baselines)]; a baseline
        is drawn as a dashed reference line across that metric's panel.
    title: the takeaway, in one short sentence; subtitle: one muted line under it.
        A title that is the ablation's question is replaced by its takeaway.
    labels: the short name each value is labelled with by its dot; default: the
        default value, the reference point when the ablation names no `reference`.
    reference: the value equal to the baseline (default: the registry's), drawn and
        labelled bold.
    x_money: x is cost per 1,000 `unit` in US dollars (otherwise tokens per page).
    cost_axis: the cost axis's title when x is one step's cost rather than the whole
        pipeline's ("Classification cost per 1,000 packets ($)"); "" for the default.
    mcoe: accepted for the renderer and ignored: the marginal cost of error is in the
        tables, and a line across the plot for it read as data.
    prices: a price-list note for a chart that shows cost, or "" for one that does not.
    families: color and shape each point by model family, with a legend.
    A y metric in RATIO_METRICS is written as a multiple ("2.5×").

    Every panel has its own x range, padded, from $0 unless the points would leave
    most of it empty. The panels of one metric always share a y range. Every point
    is named directly; a chart too crowded for that splits each dataset's panel into
    one panel per model (or model family), sharing its axes, and never numbers its
    points. Every panel sits in its own <g transform>, so the SVG keeps one <style>
    and one root that the docs site can inline.
    """
    del mcoe, runs
    all_values = list(dict.fromkeys(v for g in groups for v, *_ in g[2]))
    ab = _ablation(title, all_values)
    model = _single_model(ab, all_values)
    title = _model_title(_chart_title(title, ab), model)
    labels, default = chart_labels(
        all_values, labels or {}, default, ab, reference, bool(model)
    )
    subtitle, notes, foot = _chart_texts(subtitle, groups, ab, unit, title)
    groups = [
        (_with_unit(g[0], unit), note, g[2], g[3])
        for g, note in zip(groups, notes, strict=True)
    ]

    def x_exact(v: float) -> str:
        if x_money and cost_axis:
            return f"{money(v)} {lower_name(cost_axis.removesuffix(' ($)'))}"
        if x_money:
            return f"{money(v)} per 1,000 {unit}"
        return f"{fmt(v)} {lower_name(x_name)}"

    x_title = (
        f"{cost_axis or f'Cost per 1,000 {unit} ($)'} — cheaper ←"
        if x_money
        else f"{x_name} — fewer ←"
    )
    names = {k: name for k, name, _, _ in ys}
    cells = [
        (key, g)
        for key, *_ in ys
        for g in groups
        if any(key in yy for _, _, yy in g[2])
    ]

    # One y range per metric over all its panels.
    y_axes: dict[tuple[str, int], list[float]] = {}
    for key, _, _, percent in ys:
        found = [[yy[key] for _, _, yy in g[2] if key in yy] for g in groups]
        if not any(found):
            continue
        extra = [[g[3][key][0]] if key in g[3] else [] for g in groups]
        for i, t in enumerate(_own_y(found, extra, percent)):
            y_axes[(key, i)] = t

    def panel(
        key: str,
        gi: int,
        *,
        width: float,
        plot_h: float,
        head_h: float,
        header: str,
        note: str,
        x_axis: tuple[list[float], bool],
        x_label: str,
        y_label: bool,
        pts: list[Point],
        texts: dict[str, str],
        failures: list[str],
    ) -> str:
        xt, broken = x_axis
        return _scatter_panel(
            width=width,
            plot_h=plot_h,
            head_h=head_h,
            header=header,
            note=note,
            xt=xt,
            x_ticks=_money_ticks(xt) if x_money else [_compact(t, xt[-1]) for t in xt],
            broken=broken,
            x_title=x_label,
            yt=y_axes[(key, gi)],
            y_title=next(t for k, _, t, _ in ys if k == key) if y_label else "",
            percent=next(p for k, _, _, p in ys if k == key),
            y_name=names[key],
            points=[(v, x, yy[key]) for v, x, yy in pts if key in yy],
            labels=texts,
            default=default,
            baseline=groups[gi][3].get(key),
            failures=failures,
            colored=families,
            x_exact=x_exact,
            tips=labels,
            ratio=key in RATIO_METRICS,
        )

    def ref_key(texts: dict[str, str], top: float) -> tuple[dict[str, str], str]:
        """The labels without the reference's "(recommended)", and one key line
        saying what its bold, outlined mark is; or the labels as they are and ""."""
        word = re.search(
            r"\((recommended|reference|baseline setup|baseline)\)",
            labels.get(default or "", ""),
        )
        if default not in texts or not word:
            return texts, ""
        texts = {
            **texts,
            default: re.sub(rf"\s*\({word[1]}\)", "", texts[default]).strip(),
        }
        # The reference of an ablation run from a baseline is the baseline setup,
        # unless its own name calls it recommended.
        name = (
            "Recommended"
            if word[1] == "recommended"
            else "Baseline setup"
            if _baseline(ab)
            else "Reference"
        )
        fam = family(default) if families else "other"
        svg = marker(
            SHAPES[fam] if families else "circle",
            6,
            top + 12,
            5,
            f"dot ref fam-{fam}" if families else "dot ref",
        ) + (
            f'<text class="plabel dflt" x="16" y="{top + 16:.1f}">{escape(name)}</text>'
        )
        return texts, svg

    def render(keyed: bool = False) -> tuple[list[str], float, list[str]]:
        """Each dataset's panel side by side, two to a row; keyed, the reference's
        "(recommended)" moves from its label to a key line."""
        failures: list[str] = []
        cols = 1 if len(cells) == 1 else 2
        width = WIDTH if cols == 1 else (WIDTH - COL_GAP) / 2
        plot_h = SC_PLOT_H_WIDE if cols == 1 else SC_PLOT_H
        max_ticks = 8 if cols == 1 else 6
        x_axes = [
            cost_ticks([x["mean"] for _, x, _ in g[2]], max_ticks) for g in groups
        ]
        body: list[str] = []
        head, top = heading_svg(title, subtitle)
        body.append(head)
        if families:
            present = [
                f for f in FAMILY_ORDER if any(family(v) == f for v in all_values)
            ]
            body.append(family_legend(present, all_values, top + 16))
            top += LEGEND_H
        texts = labels
        if keyed:
            texts, key_svg = ref_key(labels, top)
            body.append(key_svg)
            top += LEGEND_H if key_svg else 0
        top += 8
        rows = [cells[i : i + cols] for i in range(0, len(cells), cols)]
        y = top
        for r, row in enumerate(rows):
            # Dataset headers on the first row only when later rows repeat its columns.
            headers = [
                g[0] if len(groups) > 1 and (r == 0 or len(ys) == 1) else ""
                for _, g in row
            ]
            row_notes = [
                g[1] if h else "" for h, (_, g) in zip(headers, row, strict=True)
            ]
            head_h = (PANEL_HEAD_H if any(headers) else 0) + (
                PANEL_NOTE_H if any(row_notes) else 0
            )
            for col, ((key, group), header, note) in enumerate(
                zip(row, headers, row_notes, strict=True)
            ):
                gi = groups.index(group)
                svg = panel(
                    key,
                    gi,
                    width=width,
                    plot_h=plot_h,
                    head_h=head_h,
                    header=header,
                    note=note,
                    x_axis=x_axes[gi],
                    x_label=x_title,
                    y_label=True,
                    pts=group[2],
                    texts=texts,
                    failures=failures,
                )
                ox = col * (width + COL_GAP)
                body.append(f'<g transform="translate({ox:.1f} {y:.1f})">{svg}</g>')
            y += head_h + Y_TITLE_H + 6 + plot_h + SC_BOTTOM
            if r < len(rows) - 1:
                y += ROW_GAP
        return body, y, failures

    def render_split(
        facets: tuple[list[str], dict[str, str], dict[str, str]],
        own_x: bool = False,
        cols: int = 0,
    ) -> tuple[list[str], float, list[str]]:
        """A row per dataset and metric, a panel per model in it, sharing its y axis
        and, unless own_x (when the shared one leaves a panel too crowded to label),
        its x axis."""
        order, of, within = facets
        failures: list[str] = []
        n = min(len(order), cols or len(order))
        width = (WIDTH - (n - 1) * FACET_GAP) / n
        x_axes = {
            (gi, f): cost_ticks(
                [x["mean"] for v, x, _ in g[2] if not own_x or of[v] == f]
                or [x["mean"] for _, x, _ in g[2]],
                4 if n > 2 else 5,
            )
            for gi, g in enumerate(groups)
            for f in order
        }
        body: list[str] = []
        head, top = heading_svg(title, subtitle)
        body.append(head)
        # A narrow panel has no room for "(recommended)" by the reference point, so
        # one key line under the subtitle says what the bold, outlined mark is.
        within, key_svg = ref_key(within, top)
        if key_svg:
            body.append(key_svg)
            top += LEGEND_H
        y = top + 8
        for r, (key, group) in enumerate(cells):
            gi = groups.index(group)
            if len(groups) > 1:
                body.append(
                    f'<text class="panel" x="0" y="{y + 14:.1f}">'
                    f"{escape(group[0])}</text>"
                )
                y += PANEL_HEAD_H + 4
                if group[1]:
                    body.append(
                        f'<text class="sub" x="0" y="{y + 9:.1f}">'
                        f"{escape(group[1])}</text>"
                    )
                    y += PANEL_NOTE_H
            present = [
                f for f in order if any(of[p[0]] == f and key in p[2] for p in group[2])
            ]
            chunks = [present[i : i + n] for i in range(0, len(present), n)]
            for c, chunk in enumerate(chunks):
                for col, facet in enumerate(chunk):
                    pts = [p for p in group[2] if of[p[0]] == facet and key in p[2]]
                    svg = panel(
                        key,
                        gi,
                        width=width,
                        plot_h=FACET_PLOT_H,
                        head_h=PANEL_HEAD_H,
                        header=facet,
                        note="",
                        x_axis=x_axes[(gi, facet)],
                        x_label="",
                        y_label=col == 0,
                        pts=pts,
                        texts=within,
                        failures=failures,
                    )
                    ox = col * (width + FACET_GAP)
                    body.append(f'<g transform="translate({ox:.1f} {y:.1f})">{svg}</g>')
                plot_bottom = y + PANEL_HEAD_H + Y_TITLE_H + 6 + FACET_PLOT_H
                body.append(
                    f'<text class="axislabel" x="{WIDTH / 2:.1f}" '
                    f'y="{plot_bottom + 36:.1f}" text-anchor="middle">'
                    f"{escape(x_title)}</text>"
                )
                y = plot_bottom + SC_BOTTOM
                if c < len(chunks) - 1:
                    y += ROW_GAP
            if r < len(cells) - 1:
                y += ROW_GAP
        return body, y, failures

    body, y, failures = render()
    facets = _facets(all_values, labels, families) if failures else None

    def cost(f: list[str]) -> tuple[int, int, int]:
        return (
            sum(x.startswith("?") for x in f),
            sum(x.startswith("!") for x in f),
            len(f),
        )

    if failures and not facets:
        keyed = render(keyed=True)
        if cost(keyed[2]) < cost(failures):
            body, y, failures = keyed
    if facets:
        # Shared x first; then each panel its own; then two panels to a row, for
        # labels too long for a third of the width.
        tries = [(False, 0), (True, 0), (False, 2), (True, 2)]
        best = None
        for own_x, cols in tries:
            got = render_split(facets, own_x, cols)
            if best is None or cost(got[2]) < cost(best[2]):
                best = got
            if not any(f[:1] in "!?" for f in got[2]):
                break
        body, y, failures = best
    wrong = [f[1:] for f in failures if f.startswith("?")]
    if wrong:
        raise ValueError(
            f"{title}: labels nearer another point than their own: {wrong}"
        )
    foot_lines = wrap_subtitle(foot, WIDTH * SUB_CHAR_W / 5.0) if foot else []
    if foot_lines:
        y += 8
    for line in foot_lines:
        body.append(
            f'<text class="prices" x="0" y="{y + 11:.1f}">{escape(line)}</text>'
        )
        y += FOOT_H
    height = y + (PRICES_H if prices else 0) + 4
    body.append(prices_svg(prices, height - 5))

    def y_text(k: str, v: float) -> str:
        if k in RATIO_METRICS:
            return times(v)
        return pct(v) if next(p for kk, _, _, p in ys if kk == k) else fmt(v)

    desc_parts = [
        f"{g[0]}, {names[k]}, {g[3][k][1]}: " + y_text(k, g[3][k][0])
        for k, *_ in ys
        for g in groups
        if k in g[3]
    ]
    desc = "; ".join(
        [
            f"{header}: "
            + "; ".join(
                (f"{names[k]}: " if len(ys) > 1 else "")
                + ", ".join(
                    f"{labels.get(v, v)} {y_text(k, yy[k]['mean'])} at {x_exact(x['mean'])}"
                    for v, x, yy in pts
                    if k in yy
                )
                for k, *_ in ys
                if any(k in yy for _, _, yy in pts)
            )
            for header, _, pts, _ in groups
        ]
        + desc_parts
        + ([foot] if foot else [])
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" width="{WIDTH}" '
        f'height="{height:.0f}" viewBox="0 0 {WIDTH} {height:.0f}" role="img">'
        f"<title>{escape(title or subtitle)}</title><desc>{escape(desc)}</desc>"
        f"<style>{STYLE}</style>" + "".join(body) + "</svg>\n"
    )


#: Categorical slots for a chart whose series are the values of one setting: a color
#: and a marker shape each, so a reader who cannot tell the colors apart still can
#: tell the series apart. Three colors and three shapes; more series than that are
#: drawn as a composite (a color per one part of the value, a shape and line style
#: per the other), never as cycled colors. The rules sit beside STYLE rather than in
#: it, so a chart that draws no series carries none of them and its image does not
#: change when they do.
SERIES_SHAPES = ("circle", "square", "diamond")
#: One (color, shape) index pair per series; shape 2 and 3 also dash the trend line.
Slots = list[tuple[int, int]]
SERIES_STYLE = """
  .viz .s1 { fill: var(--fam-nova); }
  .viz .s2 { fill: var(--fam-claude); }
  .viz .s3 { fill: var(--fam-other); }
  .viz .pt { opacity: 0.75; }
  .viz .trend { fill: none; stroke-width: 2; stroke-linejoin: round; }
  .viz .trend.t1 { stroke: var(--fam-nova); }
  .viz .trend.t2 { stroke: var(--fam-claude); }
  .viz .trend.t3 { stroke: var(--fam-other); }
  .viz .trend.d2 { stroke-dasharray: 7 3; }
  .viz .trend.d3 { stroke-dasharray: 2 3; }
  .viz .swatch { stroke-width: 2; }
  .viz .dot.sized { opacity: 0.85; stroke-width: 1.5; }
  .viz .sizekey { fill: none; stroke: var(--ink-2); stroke-width: 1; }
"""
BY_PLOT_H = 260
BY_LEGEND_H = 20
#: Room under a band's name for its N.
BY_N_H = 14
#: Horizontal offset between the values' points within a band, so their error bars
#: sit side by side.
DODGE = 2 * 14 + 2


def _slots(count: int, slots: Slots | None) -> Slots:
    """The (color, shape) of each series: the given slots, or one slot each in turn."""
    if slots and len(slots) == count:
        return [(c % 3, sh % 3) for c, sh in slots]
    return [(i % 3, i % 3) for i in range(count)]


def _legend(
    series: list[str], y: float, left: float, width: float, slots: Slots | None = None
) -> tuple[str, float]:
    """A swatch and a label per series, wrapped onto as many lines as it takes.

    The swatch is the series' marker on a short stretch of its line, so a line style
    that tells two series apart is in the key too.
    """
    out: list[str] = []
    x, line = left, 0
    for value, (c, sh) in zip(series, _slots(len(series), slots), strict=True):
        w = 38 + len(value) * CHAR_W + 18
        if x > left and x + w > left + width:
            x, line = left, line + 1
        cy = y + line * BY_LEGEND_H
        out.append(
            f'<line class="trend swatch t{c + 1} d{sh + 1}" x1="{x + 5:.1f}" '
            f'x2="{x + 32:.1f}" y1="{cy - 4:.1f}" y2="{cy - 4:.1f}"/>'
        )
        out.append(marker(SERIES_SHAPES[sh], x + 18, cy - 4, 5, f"dot s{c + 1}"))
        out.append(
            f'<text class="label" x="{x + 38:.1f}" y="{cy:.1f}">{escape(value)}</text>'
        )
        x += w
    return "".join(out), (line + 1) * BY_LEGEND_H


def band_names(bands: list[int], noun: str) -> list[str]:
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


def _band_index(x: float, bands: list[int]) -> int:
    return max(i for i, lo in enumerate(bands) if x >= lo) if x >= bands[0] else -1


def _decade_bands(xs: list[float]) -> list[int]:
    """Lower bounds 1, 10, 100, ... covering xs, when the registry gives no bands."""
    top = max(xs) if xs else 1
    return [10**e for e in range(0, max(1, math.ceil(math.log10(max(top, 1)) + 1e-9)))]


#: A by-size dot's radius at the smallest and largest cost per document drawn. Area
#: is proportional to cost, so a dot four times the area cost four times as much.
SIZE_R_MIN = 4.0
SIZE_R_MAX = 14.0


def _cost_radius(costs: list[float]) -> Any:
    """Radius as a function of cost per document: area proportional to cost, with the
    largest cost at SIZE_R_MAX and no dot smaller than SIZE_R_MIN."""
    top = max(costs) if costs else 0.0

    def radius(cost: float | None) -> float:
        if cost is None or top <= 0:
            return 5.0
        return max(SIZE_R_MIN, SIZE_R_MAX * math.sqrt(max(cost, 0.0) / top))

    return radius


def _size_steps(costs: list[float]) -> list[float]:
    """Three round costs per document spanning the data: the round value nearest its
    smallest, its geometric middle and its largest, for the size key."""
    lo, hi = max(min(costs), 1e-4), max(costs)
    nice = [m * 10**e for e in range(-4, 2) for m in (1, 1.5, 2, 3, 5)]
    targets = (lo, math.sqrt(lo * hi), hi)
    steps = [min(nice, key=lambda n, t=t: abs(math.log(n / t))) for t in targets]
    return sorted(set(steps))


def _size_key(
    costs: list[float], radius: Any, y: float, thing: str = "document"
) -> tuple[str, float]:
    """ "Dot area: cost per invoice" and three reference circles, on one line."""
    steps = _size_steps(costs)
    h = 2 * SIZE_R_MAX + 6
    cy = y + h / 2
    text = f"Dot area: cost per {thing}"
    out = [f'<text class="label" x="0" y="{cy + 4:.1f}">{escape(text)}</text>']
    x = len(text) * CHAR_W + 18
    for c in steps:
        r = radius(c)
        out.append(
            f'<circle class="sizekey" cx="{x + r:.1f}" cy="{cy:.1f}" r="{r:.1f}"/>'
            f'<text class="label" x="{x + 2 * r + 6:.1f}" y="{cy + 4:.1f}">'
            f"{escape(money(c))}</text>"
        )
        x += 2 * r + 6 + len(money(c)) * CHAR_W + 20
    return "".join(out), h


def draw_by_size(
    x_title: str,
    y_title: str,
    series: list[tuple[str, list[tuple[str, float, float]]]],
    *,
    title: str = "",
    subtitle: str = "",
    slots: Slots | None = None,
    y_name: str = "",
    bands: list[int] | None = None,
    noun: str = "",
) -> str:
    """Return an SVG of one per-document score by size band, one line per value.

    series: one entry per value of the setting, [(value, [(document, x, y), ...])],
        x a per-document size (a count) and y its score.
    bands: lower bounds of the size bands (default: the registry's `strata.bands`
        for the chart's ablation, else decades); noun names what x counts.
    Each value gets one point per band, the mean of its documents' scores there with
    an error bar of two standard errors either side, and a line joining its bands;
    the documents in each band are counted once, under the band's name. A cloud of
    one dot per document read as noise; the band means are the comparison.
    title: the takeaway; subtitle: one muted line under it.
    slots: optional (color, shape) per series, for more series than there are
        colors; by default each series takes the next color and shape.
    """
    styles = _slots(len(series), slots)
    xs_all = [x for _, pts in series for _, x, _ in pts]
    if not xs_all:
        return ""
    ab = _ablation(title)
    title = _model_title(
        _chart_title(title, ab), _single_model(ab, list((ab or {}).get("values") or []))
    )
    if ab and not _baseline(ab) and not ab.get("interim"):
        subtitle = f"{subtitle} · Everything else: each dataset's preset"
    strata = (ab or {}).get("strata") or {}
    thing = str(strata.get("documents") or "documents").removesuffix("s")
    bands = [int(b) for b in (bands or strata.get("bands") or _decade_bands(xs_all))]
    noun = noun or str(strata.get("noun") or "")
    y_name = y_name or y_title
    names = band_names(bands, noun or "items")
    # Each value's mean and standard error per band, and the documents per band.
    stats: list[list[tuple[float, float, int] | None]] = []
    docs: list[set[str]] = [set() for _ in bands]
    doc_costs = _doc_costs(ab, series)
    # Each value's mean cost per document in each band, which sizes its dot.
    band_cost: list[list[float | None]] = []
    for (_, pts), costs in zip(series, doc_costs, strict=True):
        per: list[list[float]] = [[] for _ in bands]
        spent: list[list[float]] = [[] for _ in bands]
        for doc, x, y in pts:
            i = _band_index(x, bands)
            if i >= 0:
                per[i].append(y)
                docs[i].add(doc)
                if doc in costs:
                    spent[i].append(costs[doc])
        band_cost.append([statistics.fmean(c) if c else None for c in spent])
        stats.append(
            [
                (
                    statistics.fmean(v),
                    statistics.stdev(v) / math.sqrt(len(v)) if len(v) > 1 else 0.0,
                    len(v),
                )
                if v
                else None
                for v in per
            ]
        )
    used = [i for i in range(len(bands)) if docs[i]]
    if not used:
        return ""
    vals = [
        m + s * 2 * se
        for row in stats
        for cell in row
        if cell
        for m, se, _ in [cell]
        for s in (-1, 1)
    ]
    percent = (
        min(y for _, pts in series for _, _, y in pts) >= 0
        and max(y for _, pts in series for _, _, y in pts) <= 1
    )
    yt = share_ticks(vals) if percent else _span(vals, pad=0.08)
    y_dec = tick_decimals(yt)

    def y_text(v: float) -> str:
        return pct(v) if percent else f"{v:,.{y_dec}f}"

    left, right = SC_LEFT, WIDTH - SC_RIGHT_PAD
    body: list[str] = []
    head, top = heading_svg(title, _with_unit(subtitle, "documents"))
    body.append(head)
    key_svg, key_h = _legend([v for v, _ in series], top + 22, 0, right, slots)
    body.append(key_svg)
    top += key_h
    known = [c for row in band_cost for c in row if c is not None]
    radius = _cost_radius(known)
    if known:
        size_svg, size_h = _size_key(known, radius, top + 6, thing)
        body.append(size_svg)
        top += size_h
    top += 14
    body.append(
        f'<text class="axislabel" x="{SC_LEFT - 4}" y="{top + 13}">'
        f"{escape(y_title)}</text>"
    )
    y0 = top + Y_TITLE_H + 6
    y1 = y0 + BY_PLOT_H
    slot_w = (right - left) / len(used)
    dodge = min(DODGE, slot_w / (len(series) + 1))

    def px(band: int, s: int) -> float:
        centre = left + (used.index(band) + 0.5) * slot_w
        return centre + (s - (len(series) - 1) / 2) * dodge

    def py(v: float) -> float:
        return y1 - (v - yt[0]) / (yt[-1] - yt[0]) * (y1 - y0)

    for t in yt:
        body.append(
            f'<line class="grid" x1="{left}" x2="{right}" '
            f'y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
            f'<line class="axis" x1="{left - 4}" x2="{left}" '
            f'y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
            f'<text class="tick" x="{left - 7}" y="{py(t) + 4:.1f}" '
            f'text-anchor="end">{y_text(t)}</text>'
        )
    things = x_title.split(" per ")[-1].split(" (")[0] if " per " in x_title else ""
    for i in used:
        cx = left + (used.index(i) + 0.5) * slot_w
        n_text = f"N = {len(docs[i])}" + (f" {things}s" if things else "")
        body.append(
            f'<line class="axis" x1="{cx:.1f}" x2="{cx:.1f}" '
            f'y1="{y1}" y2="{y1 + 4}"/>'
            f'<text class="tick" x="{cx:.1f}" y="{y1 + 16}" '
            f'text-anchor="middle">{escape(names[i])}</text>'
            f'<text class="tick" x="{cx:.1f}" y="{y1 + 16 + BY_N_H}" '
            f'text-anchor="middle">{escape(n_text)}</text>'
        )
    x_axis_title = re.sub(r"\s*\(log scale\)", "", x_title)
    body.append(
        f'<line class="axis" x1="{left}" x2="{right}" y1="{y1}" y2="{y1}"/>'
        f'<line class="axis" x1="{left}" x2="{left}" y1="{y0}" y2="{y1}"/>'
        f'<text class="axislabel" x="{(left + right) / 2:.1f}" '
        f'y="{y1 + 36 + BY_N_H}" text-anchor="middle">{escape(x_axis_title)}</text>'
    )
    desc_parts: list[str] = []
    for s, ((value, _), row, (c, sh), costs) in enumerate(
        zip(series, stats, styles, band_cost, strict=True)
    ):
        at = [(i, row[i]) for i in used if row[i]]
        path = " ".join(
            f"{'M' if n == 0 else 'L'}{px(i, s):.1f} {py(cell[0]):.1f}"
            for n, (i, cell) in enumerate(at)
            if cell
        )
        if len(at) > 1:
            body.append(f'<path class="trend t{c + 1} d{sh + 1}" d="{path}"/>')
        for i, cell in at:
            if not cell:
                continue
            m, se, n = cell
            cx = px(i, s)
            if se:
                body.append(
                    f'<line class="trend t{c + 1}" x1="{cx:.1f}" x2="{cx:.1f}" '
                    f'y1="{py(min(yt[-1], m + 2 * se)):.1f}" '
                    f'y2="{py(max(yt[0], m - 2 * se)):.1f}"/>'
                )
            cost = costs[i]
            cost_note = f", {money(cost)} per {thing}" if cost is not None else ""
            body.append(
                f"<g><title>{escape(value)}, {escape(names[i])}: "
                f"{escape(lower_name(y_name))} {y_text(m)} ± {y_text(2 * se)}, "
                f"{n} documents{cost_note}</title>"
                + marker(
                    SERIES_SHAPES[sh], cx, py(m), radius(cost), f"dot sized s{c + 1}"
                )
                + "</g>"
            )
        desc_parts.append(
            f"{value}: "
            + ", ".join(
                f"{names[i]} {y_text(cell[0])} ± {y_text(2 * cell[1])}"
                for i, cell in at
                if cell
            )
        )
    height = y1 + SC_BOTTOM + BY_N_H
    prices = _prices_note(ab) if known else ""
    if prices:
        height += PRICES_H
        body.append(prices_svg(prices, height - 5))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" width="{WIDTH}" '
        f'height="{height:.0f}" viewBox="0 0 {WIDTH} {height:.0f}" role="img">'
        f"<title>{escape(title or y_title)}</title>"
        f"<desc>{escape('; '.join(desc_parts))}</desc>"
        f"<style>{STYLE}{SERIES_STYLE}</style>" + "".join(body) + "</svg>\n"
    )
