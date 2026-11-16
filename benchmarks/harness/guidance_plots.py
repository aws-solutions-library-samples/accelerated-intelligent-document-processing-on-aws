"""Draw the plots for the "Benchmarks by Problem Set" pages.

render_guidance.py calls one of two functions for each ablation that has measured cells:

- draw_scatter() when the ablation reports cost or tokens: a score against cost per
  1,000 documents (or tokens per page), one panel per dataset side by side, a point
  per value labelled by its short name, and a soft band behind each dot spanning two
  standard errors either side. The chart's title is the takeaway and one muted line
  under it says what was measured, what varies and what was held fixed. Scores are
  drawn as percents on a scale that ends at 100%, and costs on round dollar ticks
  from $0 (or from a round value, with a broken-axis mark, when the points would
  otherwise crowd one end). The panels of a chart share their axes, so equal gaps mean
  equal differences, unless the datasets' costs differ more than tenfold. When x is
  cost and y an accuracy, a line runs from the cheapest value to each measurably more
  accurate value no other value beats on both axes, labelled with the marginal cost of
  error. A metric with a baseline (random review, for errors caught) gets it as a
  dashed reference line. When the values are models, each point takes its family's
  color and shape, listed in a legend.
- draw_by_size() for an ablation with a `by_size` key: one per-document score against
  the document's own size on a log x axis, one point per document per value, colored
  and shaped per value, with each value's running median over log-spaced bins as a
  line. It answers where one value overtakes another, which a mean over a dataset
  cannot show.
- draw_plot() otherwise: one metric, a panel per dataset, a row per value, a dot at the
  mean and a band spanning two standard errors either side.

Axis titles carry the direction that is better (↑ or ←), so no chart needs a
"better" note. Each SVG is static (no script), follows the light or dark color scheme,
and carries a <title> (the takeaway) and a <desc> for screen readers; the results
table under it holds every number.
"""

from __future__ import annotations

import math
import statistics
from html import escape
from typing import Any

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
  .viz .plabel.dflt { fill: var(--ink); font-weight: 600; }
  .tick { font-size: 11px; fill: var(--muted); font-variant-numeric: tabular-nums; }
  .none { font-size: 12px; fill: var(--muted); }
  .grid { stroke: var(--grid); stroke-width: 1; }
  .axis { stroke: var(--axis); stroke-width: 1; }
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
) -> str:
    """Return an SVG document.

    panels: [(dataset name, models line, [(value label, {"mean", "se"} or None), ...])]
    prices: a price-list note for a chart that shows cost, or "" for one that does not.
    """
    points = [p for _, _, rows in panels for _, p in rows if p]
    lo = min(p["mean"] - 2 * p.get("se", 0) for p in points)
    hi = max(p["mean"] + 2 * p.get("se", 0) for p in points)
    pad = (hi - lo) * 0.08 or abs(hi) * 0.05 or 1.0
    ticks = nice_ticks(lo - pad, hi + pad)
    d0, d1 = ticks[0], ticks[-1]

    decimals = tick_decimals(ticks)

    def tick_text(t: float) -> str:
        return f"{t:,.{decimals}f}"

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
                    f"<g><title>{escape(value)}: {fmt(m)} ± {fmt(2 * s)}</title>"
                    f'<rect class="band" x="{x(m - 2 * s):.1f}" '
                    f'y="{cy - BAND:.1f}" width="{x(m + 2 * s) - x(m - 2 * s):.1f}" '
                    f'height="{2 * BAND}" rx="{BAND}"/>'
                    f'<circle class="dot" cx="{x(m):.1f}" cy="{cy:.1f}" r="5"/></g>'
                )
                body.append(
                    f'<text class="label" x="{x(m + 2 * s) + 8:.1f}" y="{cy + 4:.1f}">'
                    f"{fmt(m)}</text>"
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
            f"{v} {fmt(p['mean'])}" if p else f"{v} not measured" for v, p in rows
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
#: Above this many points in one panel, points are numbered and listed in a key.
MAX_LABELLED = 12
KEY_LINE_H = 16
#: Datasets whose costs differ by more than this factor get their own cost axes.
SHARED_X_MAX_RATIO = 10.0
#: A cost axis starts at $0 unless its points would fill less than this share of it.
MIN_X_FILL = 0.2
#: Average advance of a 12px system-ui point label, regular and semibold, measured in
#: the browser with a little to spare, so a label's box is close to its ink.
LABEL_CHAR_W = 6.0
LABEL_CHAR_W_BOLD = 6.5
#: The same for an 11px label on a line (cost of error, a baseline).
SMALL_CHAR_W = 5.6


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


def cost_ticks(xs: list[float], max_ticks: int = 6) -> tuple[list[float], bool]:
    """Round ticks for a cost or count axis, and whether the axis is broken.

    The axis starts at 0 unless the points would then fill less than MIN_X_FILL of
    its width; it then starts at a round value below the smallest point, and the
    second value returned is True so the chart can draw a broken-axis mark.
    """
    lo, hi = min(xs), max(xs)
    hi = hi if hi > 0 else 1.0
    step = round_step(hi * 1.04, max_ticks)
    top = math.ceil(hi * 1.04 / step) * step
    if lo > 0 and (hi - lo) / top < MIN_X_FILL:
        pad = max((hi - lo) * 0.25, hi * 0.01)
        a, b = lo - pad, hi + pad
        step = round_step(b - a, max_ticks)
        start = math.floor(a / step) * step
        if start > 0:
            return _ticks(start, math.ceil(b / step) * step, step), True
    return _ticks(0.0, top, step), False


def x_axes_differ(xs_by_panel: list[list[float]]) -> bool:
    """Whether panels' costs differ by more than SHARED_X_MAX_RATIO (own axes each)."""
    maxes = [max(xs) for xs in xs_by_panel if xs]
    return (
        len(maxes) > 1
        and min(maxes) > 0
        and max(maxes) / min(maxes) > (SHARED_X_MAX_RATIO)
    )


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
#: Line height of a column of labels, and the gap between a cluster and its column.
COLUMN_STEP = 15
COLUMN_GAP = 26
#: Labels whose points are further apart than this, in x, get separate columns.
CLUSTER_GAP = 60

Label = tuple[str, float, float, str, tuple[float, float] | None]


def _inflate(r: Rect, by: float, by_y: float | None = None) -> Rect:
    dy = by if by_y is None else by_y
    return (r[0] - by, r[1] - dy, r[2] + by, r[3] + dy)


def _column(
    pending: list[tuple[str, str, float, float, float]],
    bounds: Rect,
    blocked: list[Rect],
    labels: list[Rect],
    all_points: list[tuple[str, str, float, float, float]],
) -> dict[str, Label] | None:
    """Labels stacked in a column beside their points, in the points' vertical order.

    Each label sits as near its point's height as the stack allows and has a leader
    line back to it; ordering the column by height means no two leaders cross. The
    column goes to the right of the points, or to the left when there is no room,
    and moves outward until no label in it overlaps anything in blocked and no leader
    crosses one of the labels already placed. Returns None when no column fits.
    """
    x0, y0, x1, y1 = bounds
    rows = sorted(pending, key=lambda p: (p[3], p[2]))
    dots = {key: (cx - 6, cy - 6, cx + 6, cy + 6) for key, _, cx, cy, _ in all_points}
    mine = {key for key, *_ in rows}
    # A label in the column keeps clear of the dots that are not in it, so it is not
    # read as one of theirs.
    others = [_inflate(d, OWN_GAP) for k, d in dots.items() if k not in mine]
    # A leader may pass a dot that has its own leader in the column, not one without.
    outside = [d for k, d in dots.items() if k not in mine]
    widths = [len(t) * cw for _, t, _, _, cw in rows]
    for side in (1, -1):
        edge = max(p[2] for p in rows) if side > 0 else min(p[2] for p in rows)
        for shift in range(0, 200, 12):
            lx = edge + side * (COLUMN_GAP + shift)
            anchor = "start" if side > 0 else "end"
            if side > 0 and lx + max(widths) > x1 or side < 0 and lx - max(widths) < x0:
                break
            ys: list[float] = []
            for _, _, _, cy, _ in rows:
                ys.append(max(cy + 4, ys[-1] + COLUMN_STEP) if ys else cy + 4)
            # Centre the stack on its points, so the leaders are as short as they
            # can be on average, then keep it inside the plot.
            centre = statistics.fmean(p[3] + 4 for p in rows) - statistics.fmean(ys)
            ys = [y + centre for y in ys] if centre < 0 else ys
            lift = (y0 + 11) - ys[0]
            if lift > 0:
                ys = [y + lift for y in ys]
            over = ys[-1] - (y1 - 1)
            if over > 0:
                ys = [y - over for y in ys]
            if ys[0] - 11 < y0:
                return None
            rects = [_rect(lx, y, w, anchor) for y, w in zip(ys, widths, strict=True)]
            if any(_hits(r, blocked + others) for r in rects):
                continue
            out: dict[str, Label] = {}
            crossed = False
            for (key, text, cx, cy, _), y, r in zip(rows, ys, rects, strict=True):
                end = _leader_end(cx, cy, r)
                path = _segment_boxes(cx, cy, *end)[2:]
                crossed = crossed or _hits_any(path, labels + outside)
                out[key] = (text, lx, y, anchor, end)
            if crossed:
                continue
            return out
    return None


#: A cluster of at most this many labels first tries a label each above or below its
#: point, which reads more directly than a column.
SMALL_CLUSTER = 3


def _fit(cx: float, w: float, x0: float, x1: float) -> tuple[float, str]:
    """A label centred over cx, or anchored to the plot edge it would cross, so its
    text ends over its point rather than drifting toward a neighbour's."""
    if cx + w / 2 > x1:
        return x1, "end"
    if cx - w / 2 < x0:
        return x0, "start"
    return cx, "middle"


def _above_below(
    cluster: list[tuple[str, str, float, float, float]],
    bounds: Rect,
    blocked: list[Rect],
    dots: dict[str, Rect],
) -> dict[str, Label] | None:
    """A label each just above or below its point, shifted sideways to fit the plot,
    with a leader that passes no other dot. None unless every label finds a spot."""
    x0, _, x1, _ = bounds
    taken: list[Rect] = []
    out: dict[str, Label] = {}
    for key, text, cx, cy, cw in sorted(cluster, key=lambda p: (p[3], p[2])):
        w = len(text) * cw
        others = [d for k, d in dots.items() if k != key]
        lx, anchor = _fit(cx, w, x0, x1)
        for dy in (-12, 22, -26, 36, -40, 50, -54, 64):
            r = _rect(lx, cy + dy, w, anchor)
            if not _clear(
                r, bounds, blocked + taken + [_inflate(d, 3) for d in others]
            ):
                continue
            end = _leader_end(cx, cy, r)
            if _hits_any(_segment_boxes(cx, cy, *end)[2:], others + taken):
                continue
            leader = end if math.hypot(end[0] - cx, end[1] - cy) > 9 else None
            out[key] = (text, lx, cy + dy, anchor, leader)
            taken.append(r)
            break
        else:
            return None
    return out


def _place_labels(
    points: list[tuple[str, str, float, float, float]],
    bounds: Rect,
    obstacles: list[Rect],
    soft: list[Rect] | None = None,
) -> dict[str, Label]:
    """Place one label per point without overlaps.

    points: (key, text, x, y, character width). A label first tries the spots right
    beside its point, taking one only if it keeps clear of every other dot. Of the
    labels that found none, a small cluster tries a label each above or below its
    point (_above_below); a larger one, or one that fails, is stacked in a column
    beside its points with leader lines (_column), or failing that every label in its
    neighbourhood is. A label that still
    has no spot takes the nearest clear one above or below its point, shifted to fit
    the plot. `soft` obstacles (a line the labels' halos can cross) are kept clear
    only by the labels beside their dots. Returns {key: (text, x, y, anchor, leader
    end or None)}.
    """
    dots = {key: (cx - 7, cy - 7, cx + 7, cy + 7) for key, _, cx, cy, _ in points}
    taken: list[Rect] = []
    out: dict[str, Label] = {}
    pending = []
    for key, text, cx, cy, cw in sorted(points, key=lambda p: (p[3], p[2])):
        w = len(text) * cw
        others = [_inflate(d, OWN_GAP, OWN_GAP_Y) for k, d in dots.items() if k != key]
        for dx, dy, anchor in NEAR:
            r = _rect(cx + dx, cy + dy, w, anchor)
            if _clear(r, bounds, taken + others + obstacles + (soft or [])):
                taken.append(r)
                out[key] = (text, cx + dx, cy + dy, anchor, None)
                break
        else:
            pending.append((key, text, cx, cy, cw))
    if not pending:
        return out
    # Each cluster of labels that found no spot gets a column; failing that, every
    # label in the cluster's neighbourhood (whose near labels may be in the way) does.
    pending.sort(key=lambda p: p[2])
    clusters: list[list[tuple[str, str, float, float, float]]] = []
    for p in pending:
        if clusters and p[2] - clusters[-1][-1][2] <= CLUSTER_GAP:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    def rect_of(label: Label) -> Rect:
        text, lx, ly, anchor, _ = label
        return _rect(lx, ly, len(text) * LABEL_CHAR_W_BOLD, anchor)

    for cluster in clusters:
        if len(cluster) <= SMALL_CLUSTER:
            taken = [rect_of(lab) for lab in out.values()]
            stacked = _above_below(cluster, bounds, taken + obstacles, dots)
            if stacked is not None:
                out.update(stacked)
                continue
        lo = min(p[2] for p in cluster) - 40
        hi = max(p[2] for p in cluster) + 40
        top = min(p[3] for p in cluster) - 50
        bottom = max(p[3] for p in cluster) + 50
        nearby = [p for p in points if lo <= p[2] <= hi and top <= p[3] <= bottom]
        for group in (cluster, nearby):
            keys = {p[0] for p in group}
            kept = [rect_of(lab) for k, lab in out.items() if k not in keys]
            column = _column(group, bounds, kept + obstacles, kept, points)
            if column is not None:
                for k in keys:
                    out.pop(k, None)
                out.update(column)
                break
    taken = [rect_of(lab) for lab in out.values()]
    x0, _, x1, _ = bounds
    for key, text, cx, cy, cw in pending:
        if key in out:
            continue
        w = len(text) * cw
        spot = None
        others = [_inflate(d, OWN_GAP) for k, d in dots.items() if k != key]
        lx, anchor = _fit(cx, w, x0, x1)
        for dy in sorted(range(-160, 161, 14), key=abs):
            if abs(dy) < 12:
                continue
            r = _rect(lx, cy + dy, w, anchor)
            if _clear(r, bounds, taken + obstacles + others):
                spot = (text, lx, cy + dy, anchor, _leader_end(cx, cy, r))
                break
        if spot is None:
            spot = (text, lx, cy - 12, anchor, None)
        taken.append(_rect(spot[1], spot[2], w, anchor))
        out[key] = spot
    return out


def _hits_any(boxes: list[Rect], others: list[Rect]) -> bool:
    return any(_hits(b, others) for b in boxes)


def _place_on_line(
    text: str, a: tuple[float, float], b: tuple[float, float], bounds: Rect, blocked
) -> tuple[float, float] | None:
    """A spot for a label on a line, near its middle, clear of everything blocked."""
    w = len(text) * SMALL_CHAR_W
    for t in (0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74, 0.18, 0.82):
        mx, my = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
        for dy in (4, -7, 15):
            if _clear(_rect(mx, my + dy, w, "middle"), bounds, blocked):
                return mx, my + dy
    return None


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
    lines: list[tuple[str, str, str]],
    baseline: tuple[float, str] | None,
    numbers: dict[str, int] | None,
    colored: bool,
    x_exact: Any,
) -> str:
    """One scatter panel in its own coordinates.

    points: (value, x, y); lines: (label, from value, to value) for the marginal cost
    of error, each drawn only if its label finds a spot on it; numbers, when given,
    replace the names by the dots (more than MAX_LABELLED points).
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
        return pct(v) if percent else fmt(v)

    body: list[str] = []
    if header:
        body.append(f'<text class="ptitle" x="0" y="14">{escape(header)}</text>')
    if note:
        body.append(
            f'<text class="sub" x="0" y="{PANEL_HEAD_H + 11}">{escape(note)}</text>'
        )
    body.append(
        f'<text class="axislabel" x="0" y="{head_h + 13}">{escape(y_title)}</text>'
    )
    y_dec = tick_decimals(yt)
    for t in yt:
        text = pct(t) if percent else f"{t:,.{y_dec}f}"
        body.append(
            f'<line class="grid" x1="{left}" x2="{right}" '
            f'y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
            f'<text class="tick" x="{left - 8}" y="{py(t) + 4:.1f}" '
            f'text-anchor="end">{text}</text>'
        )
    for t, text in zip(xt, x_ticks, strict=True):
        body.append(
            f'<text class="tick" x="{px(t):.1f}" y="{y1 + 16}" '
            f'text-anchor="middle">{text}</text>'
        )
    body.append(
        f'<line class="axis" x1="{left}" x2="{right}" y1="{y1}" y2="{y1}"/>'
        f'<line class="axis" x1="{left}" x2="{left}" y1="{y0}" y2="{y1}"/>'
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
    obstacles: list[Rect] = [(0, head_h, len(y_title) * 6.2, head_h + 18)]
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
        obstacles.append(_rect(right - 4, by - 4, w, "end"))
        tail.append(
            f'<text class="baselinelabel halo" x="{right - 4:.1f}" y="{by - 4:.1f}" '
            f'text-anchor="end">{escape(name[:1].upper() + name[1:])}</text>'
        )
    centre = {v: (px(x["mean"]), py(y["mean"])) for v, x, y in points}
    for value, _, y in points:
        cx, cy = centre[value]
        dy = 2 * y.get("se", 0)
        band_top = max(y0, py(y["mean"] + dy))
        band_bottom = min(y1, py(y["mean"] - dy))
        obstacles.append((cx - 7, cy - 7, cx + 7, cy + 7))
        if band_bottom > band_top:
            cls = f"band fam-{family(value)}" if colored else "band"
            body.append(
                f'<rect class="{cls}" x="{cx - BAND:.1f}" y="{band_top:.1f}" '
                f'width="{2 * BAND}" height="{band_bottom - band_top:.1f}" '
                f'rx="{BAND}"/>'
            )
    line_boxes = [
        box
        for _, ref, value in lines
        for box in _segment_boxes(*centre[ref], *centre[value])
    ]
    bounds = (left + 2, y0 - 8, right, y1 - 2)
    shown = {v: str(numbers[v]) if numbers else labels.get(v, v) for v, _, _ in points}
    placed = _place_labels(
        [
            (
                v,
                shown[v],
                *centre[v],
                LABEL_CHAR_W_BOLD if v == default else LABEL_CHAR_W,
            )
            for v, _, _ in points
        ],
        bounds,
        obstacles,
        line_boxes,
    )
    blocked = obstacles + [
        _rect(lx, ly, len(text) * LABEL_CHAR_W_BOLD, anchor)
        for text, lx, ly, anchor, _ in placed.values()
    ]
    for text, ref, value in lines:
        spot = _place_on_line(text, centre[ref], centre[value], bounds, blocked)
        if spot is None:
            continue  # no room for its label: the table carries the figure
        blocked.append(_rect(spot[0], spot[1], len(text) * SMALL_CHAR_W, "middle"))
        (ax, ay), (bx, by) = centre[ref], centre[value]
        body.append(
            f"<g><title>{escape(labels.get(value, value))} against "
            f"{escape(labels.get(ref, ref))}: {escape(text)}</title>"
            f'<line class="mcoe" x1="{ax:.1f}" y1="{ay:.1f}" '
            f'x2="{bx:.1f}" y2="{by:.1f}"/></g>'
        )
        tail.append(
            f'<text class="mcoelabel halo" x="{spot[0]:.1f}" y="{spot[1]:.1f}" '
            f'text-anchor="middle">{escape(text)}</text>'
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
        body.append(
            f"<g><title>{escape(labels.get(value, value))}: {escape(y_name)} "
            f"{escape(y_text(y['mean']))} ± {escape(y_text(2 * y.get('se', 0)))}, "
            f"{escape(x_exact(x['mean']))}</title>"
            + marker(SHAPES[fam] if colored else "circle", cx, cy, 5, cls)
            + "</g>"
        )
    for value, (text, lx, ly, anchor, _) in placed.items():
        cls = "plabel dflt" if value == default else "plabel"
        body.append(
            f'<text class="{cls} halo" x="{lx:.1f}" y="{ly:.1f}" '
            f'text-anchor="{anchor}">{escape(text)}</text>'
        )
    return "".join(body + tail)


def lower_name(name: str) -> str:
    """Lower-case a metric name mid-sentence, leaving an acronym such as AUROC alone."""
    return name if name[:2].isupper() else name[:1].lower() + name[1:]


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
) -> str:
    """Return an SVG document plotting each y metric against x (cost or tokens).

    ys: the metrics on the y-axis, a row of panels each.
    groups: one panel per dataset, [(header, note, points, baselines)]; a baseline
        is drawn as a dashed reference line across that metric's panel.
    title: the takeaway, in one short sentence; subtitle: one muted line under it.
    labels: the short name each value is labelled with by its dot; default: the
        default value, whose label is marked.
    x_money: x is cost per 1,000 `unit` in US dollars (otherwise tokens per page).
    mcoe: the y metrics that get marginal-cost-of-error lines from the cheapest value
        to each measurably more accurate value no other value beats on both axes.
    prices: a price-list note for a chart that shows cost, or "" for one that does not.
    families: color and shape each point by model family, with a legend.

    The panels of one metric share their y range, and every panel shares one x range
    unless the datasets' costs differ by more than SHARED_X_MAX_RATIO. Every panel sits
    in its own <g transform>, so the SVG keeps one <style> and one root that the docs
    site can inline.
    """
    labels = labels or {}

    def x_exact(v: float) -> str:
        if x_money:
            return f"{money(v)} per 1,000 {unit}"
        return f"{fmt(v)} {lower_name(x_name)}"

    x_title = (
        f"Cost per 1,000 {unit} ($) — cheaper ←" if x_money else f"{x_name} — fewer ←"
    )
    names = {k: name for k, name, _, _ in ys}
    cells = [
        (key, g)
        for key, *_ in ys
        for g in groups
        if any(key in yy for _, _, yy in g[2])
    ]
    cols = 1 if len(cells) == 1 else 2
    width = WIDTH if cols == 1 else (WIDTH - COL_GAP) / 2
    plot_h = SC_PLOT_H_WIDE if cols == 1 else SC_PLOT_H
    max_ticks = 8 if cols == 1 else 6

    # One x range for every panel, unless the datasets' costs are far apart.
    xs_by_group = [[x["mean"] for _, x, _ in g[2]] for g in groups]
    if x_axes_differ(xs_by_group):
        x_axes = [cost_ticks(xs, max_ticks) for xs in xs_by_group]
    else:
        x_axes = [cost_ticks([x for xs in xs_by_group for x in xs], max_ticks)] * len(
            groups
        )

    # One y range per metric, over every panel.
    y_axes: dict[str, list[float]] = {}
    for key, _, _, percent in ys:
        found = [yy[key] for g in groups for _, _, yy in g[2] if key in yy]
        if not found:
            continue
        vals = [y["mean"] + s * 2 * y.get("se", 0) for y in found for s in (-1, 1)]
        vals += [g[3][key][0] for g in groups if key in g[3]]
        y_axes[key] = (
            share_ticks(vals) if percent else _y_ticks(vals, [y["mean"] for y in found])
        )

    numbered = any(len(g[2]) > MAX_LABELLED for g in groups)
    numbers = (
        {
            v: i
            for i, v in enumerate(dict.fromkeys(v for g in groups for v, *_ in g[2]), 1)
        }
        if numbered
        else None
    )

    body: list[str] = []
    head, top = heading_svg(title, subtitle)
    body.append(head)
    all_values = list(dict.fromkeys(v for g in groups for v, *_ in g[2]))
    if families:
        present = [f for f in FAMILY_ORDER if any(family(v) == f for v in all_values)]
        body.append(family_legend(present, all_values, top + 16))
        top += LEGEND_H
    top += 8
    desc_parts: list[str] = []
    rows = [cells[i : i + cols] for i in range(0, len(cells), cols)]
    y = top
    for r, row in enumerate(rows):
        # Dataset headers on the first row only when later rows repeat its columns.
        headers = [
            g[0] if len(groups) > 1 and (r == 0 or len(ys) == 1) else "" for _, g in row
        ]
        notes = [g[1] if h else "" for h, (_, g) in zip(headers, row, strict=True)]
        head_h = (PANEL_HEAD_H if any(headers) else 0) + (
            PANEL_NOTE_H if any(notes) else 0
        )
        for col, ((key, group), header, note) in enumerate(
            zip(row, headers, notes, strict=True)
        ):
            name, pts, baselines = group[0], group[2], group[3]
            xt, broken = x_axes[groups.index(group)]
            x_ticks = _money_ticks(xt) if x_money else [_compact(t, xt[-1]) for t in xt]
            percent = next(p for k, _, _, p in ys if k == key)
            own = [(v, x, yy[key]) for v, x, yy in pts if key in yy]
            lines: list[tuple[str, str, str]] = []
            if key in mcoe:
                ref, gains = marginal_cost_of_error(own)
                frontier = _frontier(own)
                lines = [
                    (mcoe_text(c), ref, v)
                    for v, c in gains.items()
                    if c is not None and v in frontier
                ]
                if gains:
                    desc_parts.append(
                        f"{name}, marginal cost of error against "
                        f"{labels.get(ref, ref)}: "
                        + ", ".join(
                            f"{labels.get(v, v)} {mcoe_text(c)}"
                            for v, c in gains.items()
                        )
                    )
            svg = _scatter_panel(
                width=width,
                plot_h=plot_h,
                head_h=head_h,
                header=header,
                note=note,
                xt=xt,
                x_ticks=x_ticks,
                broken=broken,
                x_title=x_title,
                yt=y_axes[key],
                y_title=next(t for k, _, t, _ in ys if k == key),
                percent=percent,
                y_name=names[key],
                points=own,
                labels=labels,
                default=default,
                lines=lines,
                baseline=baselines.get(key),
                numbers=numbers,
                colored=families,
                x_exact=x_exact,
            )
            ox = col * (width + COL_GAP)
            body.append(f'<g transform="translate({ox:.1f} {y:.1f})">{svg}</g>')
            if key in baselines:
                value, base = baselines[key]
                desc_parts.append(
                    f"{name}, {names[key]}, {base}: "
                    + (pct(value) if percent else fmt(value))
                )
        y += head_h + Y_TITLE_H + 6 + plot_h + SC_BOTTOM
        if r < len(rows) - 1:
            y += ROW_GAP
    if numbers:
        y += 6
        for value, n in numbers.items():
            body.append(
                f'<text class="label" x="0" y="{y + 12:.1f}"><tspan class="num">{n}'
                f"</tspan>  {escape(labels.get(value, value))}</text>"
            )
            y += KEY_LINE_H
    height = y + (PRICES_H if prices else 0) + 4
    body.append(prices_svg(prices, height - 5))

    def y_text(k: str, v: float) -> str:
        return pct(v) if next(p for kk, _, _, p in ys if kk == k) else fmt(v)

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
"""
BY_PLOT_H = 240
BY_LEGEND_H = 20
#: Points in a log-spaced bin before its median joins the trend line.
BIN_MIN_POINTS = 2
#: Bins per decade of the x axis, for the running median.
BINS_PER_DECADE = 2


def _decades(lo: float, hi: float) -> list[float]:
    """Powers of ten covering [lo, hi]: the ticks of a log axis."""
    first = math.floor(math.log10(max(lo, 1e-9)))
    last = math.ceil(math.log10(max(hi, 10 ** (first + 1))))
    return [10.0**e for e in range(first, last + 1)]


def _running_median(
    points: list[tuple[float, float]], lo: float, hi: float
) -> list[tuple[float, float]]:
    """The median of the points in each log-spaced bin, left to right.

    A bin holding fewer than BIN_MIN_POINTS points is left out rather than drawn as a
    trend, so one document cannot bend the line.
    """
    span = math.log10(hi) - math.log10(lo)
    count = max(2, round(span * BINS_PER_DECADE))
    bins: list[list[tuple[float, float]]] = [[] for _ in range(count)]
    for x, y in points:
        at = (math.log10(x) - math.log10(lo)) / span * count if span else 0.0
        bins[min(count - 1, max(0, int(at)))].append((x, y))
    out = []
    for group in bins:
        if len(group) >= BIN_MIN_POINTS:
            out.append(
                (
                    statistics.median(x for x, _ in group),
                    statistics.median(y for _, y in group),
                )
            )
    return out


def _slots(count: int, slots: Slots | None) -> Slots:
    """The (color, shape) of each series: the given slots, or one slot each in turn."""
    if slots and len(slots) == count:
        return [(c % 3, sh % 3) for c, sh in slots]
    return [(i % 3, i % 3) for i in range(count)]


def _legend(
    series: list[str], y: float, left: float, width: float, slots: Slots | None = None
) -> tuple[str, float]:
    """A swatch and a label per series, wrapped onto as many lines as it takes.

    The swatch is the series' marker on a short stretch of its trend line, so a line
    style that tells two series apart is in the key too.
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
        out.append(marker(SERIES_SHAPES[sh], x + 5, cy - 4, 5, f"dot s{c + 1}"))
        out.append(
            f'<text class="label" x="{x + 38:.1f}" y="{cy:.1f}">{escape(value)}</text>'
        )
        x += w
    return "".join(out), (line + 1) * BY_LEGEND_H


def draw_by_size(
    x_title: str,
    y_title: str,
    series: list[tuple[str, list[tuple[str, float, float]]]],
    *,
    title: str = "",
    subtitle: str = "",
    slots: Slots | None = None,
    y_name: str = "",
) -> str:
    """Return an SVG plotting one per-document score against the document's size.

    series: one entry per value of the setting, [(value, [(document, x, y), ...])].
        x is a count, drawn on a log axis; y is a score, drawn as a percent when it
        lies between 0 and 1. One point per document per value, colored and shaped
        by value, with the running median of each value over log-spaced bins as a
        line, so a trend reads without a fitted curve.
    title: the takeaway; subtitle: one muted line under it.
    slots: optional (color, shape) per series, for more series than there are
        colors; by default each series takes the next color and shape.
    """
    styles = _slots(len(series), slots)
    points = [(x, y) for _, pts in series for _, x, y in pts]
    if not points:
        return ""
    y_name = y_name or y_title
    xt = _decades(min(x for x, _ in points), max(x for x, _ in points))
    ys = [y for _, y in points]
    percent = min(ys) >= 0 and max(ys) <= 1
    yt = [i / 5 for i in range(6)] if percent else _span(ys, pad=0.08)
    y_dec = tick_decimals(yt)

    def y_text(v: float) -> str:
        return pct(v) if percent else f"{v:,.{y_dec}f}"

    left, right = SC_LEFT, WIDTH - SC_RIGHT_PAD
    body: list[str] = []
    head, top = heading_svg(title, subtitle)
    body.append(head)
    key_svg, key_h = _legend([v for v, _ in series], top + 22, 0, right, slots)
    body.append(key_svg)
    top += key_h + 14
    body.append(
        f'<text class="axislabel" x="0" y="{top + 13}">{escape(y_title)}</text>'
    )
    y0 = top + Y_TITLE_H + 6
    y1 = y0 + BY_PLOT_H

    def px(v: float) -> float:
        lo, hi = math.log10(xt[0]), math.log10(xt[-1])
        return left + (math.log10(max(v, xt[0])) - lo) / (hi - lo) * (right - left)

    def py(v: float) -> float:
        return y1 - (v - yt[0]) / (yt[-1] - yt[0]) * (y1 - y0)

    for t in yt:
        body.append(
            f'<line class="grid" x1="{left}" x2="{right}" '
            f'y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
            f'<text class="tick" x="{left - 8}" y="{py(t) + 4:.1f}" '
            f'text-anchor="end">{y_text(t)}</text>'
        )
    for t in xt:
        body.append(
            f'<line class="grid" x1="{px(t):.1f}" x2="{px(t):.1f}" '
            f'y1="{y0}" y2="{y1}"/>'
            f'<text class="tick" x="{px(t):.1f}" y="{y1 + 16}" '
            f'text-anchor="middle">{t:,.0f}</text>'
        )
    body.append(
        f'<line class="axis" x1="{left}" x2="{right}" y1="{y1}" y2="{y1}"/>'
        f'<line class="axis" x1="{left}" x2="{left}" y1="{y0}" y2="{y1}"/>'
        f'<text class="axislabel" x="{(left + right) / 2:.1f}" y="{y1 + 36}" '
        f'text-anchor="middle">{escape(x_title)}</text>'
    )
    desc_parts: list[str] = []
    for (value, pts), (c, sh) in zip(series, styles, strict=True):
        for doc, x, y in pts:
            body.append(
                f"<g><title>{escape(doc)}: {escape(value)}, {x:,.0f} "
                f"{escape(lower_name(x_title.split(' (')[0]))}, "
                f"{escape(lower_name(y_name))} {y_text(y)}</title>"
                + marker(SERIES_SHAPES[sh], px(x), py(y), 3.4, f"pt s{c + 1}")
                + "</g>"
            )
    for (value, pts), (c, sh) in zip(series, styles, strict=True):
        trend = _running_median([(x, y) for _, x, y in pts], xt[0], xt[-1])
        if len(trend) > 1:
            path = " ".join(
                f"{'M' if n == 0 else 'L'}{px(x):.1f} {py(y):.1f}"
                for n, (x, y) in enumerate(trend)
            )
            body.append(
                f"<g><title>{escape(value)}: running median</title>"
                f'<path class="trend t{c + 1} d{sh + 1}" d="{path}"/></g>'
            )
        desc_parts.append(
            f"{value}: {len(pts)} documents, running median "
            + ", ".join(f"{y_text(y)} at {x:,.0f}" for x, y in trend)
        )
    height = y1 + SC_BOTTOM
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" width="{WIDTH}" '
        f'height="{height:.0f}" viewBox="0 0 {WIDTH} {height:.0f}" role="img">'
        f"<title>{escape(title or y_title)}</title>"
        f"<desc>{escape('; '.join(desc_parts))}</desc>"
        f"<style>{STYLE}{SERIES_STYLE}</style>" + "".join(body) + "</svg>\n"
    )
