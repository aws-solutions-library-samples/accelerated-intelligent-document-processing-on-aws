"""Draw the plots for the "Benchmarks by Problem Set" pages.

render_guidance.py calls one of two functions for each ablation that has measured cells:

- draw_scatter() when the ablation reports cost or tokens: accuracy against cost per
  1,000 documents (or tokens per page), a panel per dataset and a point per value, with
  a soft band behind each dot spanning two standard errors either side. Up and to the
  left is better. When x is cost and y is an accuracy, thin lines run from the cheapest
  value to each more accurate one, labelled with the marginal cost of error. Two or
  more y metrics are drawn as charts side by side; a metric with a baseline (random
  review, for errors caught) gets it as a dashed line. When the values are models,
  each point takes its family's color and shape, and each family can get its own
  chart on shared axes with the other families' points in gray behind it.
- draw_by_size() for an ablation with a `by_size` key: one per-document score against
  the document's own size on a log x axis, one point per document per value, colored
  and shaped per value, with each value's running median over log-spaced bins as a
  line. It answers where one value overtakes another, which a mean over a dataset
  cannot show.
- draw_plot() otherwise: one metric, a panel per dataset, a row per value, a dot at the
  mean and a band spanning two standard errors either side.

Each panel carries a subtitle naming the model in force at every step, in the
secondary ink so it reads as part of the chart rather than as a footnote, so a chart on
its own says which models produced it, and a chart that shows cost carries the price
list it was computed from.

Each SVG is static (no script), follows the light or dark color scheme, and carries a
<title> and <desc> for screen readers; the results table under it holds every number.
"""

from __future__ import annotations

import math
import statistics
import textwrap
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

WIDTH = 720
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
  .panel { font-size: 13px; font-weight: 600; fill: var(--ink); }
  .sub { font-size: 11px; fill: var(--muted); }
  .viz .lead { font-size: 12px; fill: var(--ink); }
  .label { font-size: 12px; fill: var(--ink-2); }
  .tick { font-size: 11px; fill: var(--muted); font-variant-numeric: tabular-nums; }
  .none { font-size: 12px; fill: var(--muted); }
  .grid { stroke: var(--grid); stroke-width: 1; }
  .axis { stroke: var(--axis); stroke-width: 1; }
  .band { fill: var(--series); opacity: 0.12; }
  .dot { fill: var(--series); stroke: var(--ring); stroke-width: 2; }
  .axislabel { font-size: 11px; fill: var(--ink-2); }
  .note { font-size: 11px; fill: var(--muted); font-style: italic; }
  .prices { font-size: 10px; fill: var(--muted); }
  .num { font-size: 11px; font-weight: 700; fill: var(--ink); }
  .halo { paint-order: stroke; stroke: var(--ring); stroke-width: 3px;
          stroke-linejoin: round; }
  .mcoe { stroke: var(--muted); stroke-width: 1; fill: none; }
  .mcoe.noise { stroke-dasharray: 4 3; }
  .mcoelabel { font-size: 11px; fill: var(--ink-2); }
  .viz .ptitle { font-size: 12px; font-weight: 600; fill: var(--ink-2); }
  .viz .keyhead { font-size: 11px; fill: var(--muted); font-style: italic; }
  .viz .fam-nova { fill: var(--fam-nova); }
  .viz .fam-claude { fill: var(--fam-claude); }
  .viz .fam-other { fill: var(--fam-other); }
  .viz .ctx { fill: var(--muted); opacity: 0.55; }
  .viz .baseline { stroke: var(--ink-2); stroke-width: 1.5; stroke-dasharray: 6 4; }
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


SC_LEFT = 64
SC_RIGHT = WIDTH - 24
SC_PLOT_H = 190
SC_BOTTOM = 44
#: Above this many points, only the default and the frontier are labelled.
MAX_LABELLED = 4
#: Width of the "Better: up and to the left" note, which labels must keep off.
NOTE_W = 150


def _span(
    values: list[float],
    *,
    min_span: float = 0.0,
    pad: float = 0.12,
    count: int = 4,
    allow_negative: bool = True,
) -> list[float]:
    """Return round ticks covering the values, padded, with a minimum span.

    The axis is sized to the values themselves, not to zero: on a cost axis two
    values $11 apart would otherwise sit on top of each other in the middle of the
    range. `min_span`, a fraction of the largest value, stops the opposite problem,
    where two close values spread across the whole axis and a small difference reads
    as the whole story. `allow_negative` is False for a cost or a count.
    """
    lo, hi = min(values), max(values)
    if min_span and hi - lo < min_span * abs(hi):
        grow = (min_span * abs(hi) - (hi - lo)) / 2
        lo, hi = lo - grow, hi + grow
    margin = (hi - lo) * pad or abs(hi) * 0.05 or 1.0
    lo, hi = lo - margin, hi + margin
    if not allow_negative:
        lo = max(0.0, lo)
    return nice_ticks(lo, hi, count)


def _compact(v: float, axis_max: float) -> str:
    """One style per axis: compact (5K, 10K) when the axis reaches 10,000."""
    if axis_max >= 10_000:
        return f"{v / 1000:,.{0 if v % 1000 == 0 else 1}f}K" if v else "0"
    return f"{v:,.0f}"


def _money_ticks(ticks: list[float]) -> list[str]:
    """Whole dollars, or cents when a tick falls between them."""
    whole = all(abs(t - round(t)) < 1e-9 for t in ticks)
    return [f"${t:,.0f}" if whole else f"${t:,.2f}" for t in ticks]


def _frontier(points: list[tuple[str, dict[str, float], dict[str, float]]]) -> set[str]:
    """Values no other value beats on both axes (fewer tokens and higher accuracy)."""
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


def _clear(r: Rect, bounds: Rect, blocked: list[Rect]) -> bool:
    x0, y0, x1, y1 = bounds
    if r[0] < x0 or r[2] > x1 or r[1] < y0 or r[3] > y1:
        return False
    return not any(
        r[0] < o[2] and o[0] < r[2] and r[1] < o[3] and o[1] < r[3] for o in blocked
    )


def _place_on_lines(
    lines: list[tuple[str, float, float, float, float]],
    bounds: Rect,
    blocked: list[Rect],
) -> list[tuple[str, float, float]]:
    """Place a label on each line, near its middle, clear of everything in blocked.

    Labels sit on the line (their halo hides it) and move along it, then just above
    or below it, until clear. A label with no clear spot is left out of the result,
    and the caller lists it in the chart's key instead.
    """
    taken = list(blocked)
    out = []
    for text, ax, ay, bx, by in lines:
        w = len(text) * CHAR_W * 0.92
        for t in (0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74, 0.18, 0.82):
            mx, my = ax + t * (bx - ax), ay + t * (by - ay)
            spot = next(
                (
                    (mx, my + dy)
                    for dy in (4, -7, 15)
                    if _clear(_rect(mx, my + dy, w, "middle"), bounds, taken)
                ),
                None,
            )
            if spot:
                taken.append(_rect(spot[0], spot[1], w, "middle"))
                out.append((text, spot[0], spot[1]))
                break
    return out


def _place_labels(
    points: list[tuple[str, float, float]],
    bounds: Rect,
    obstacles: list[Rect],
) -> list[tuple[str, float, float, str, tuple[float, float] | None]]:
    """Place one label per point without overlaps.

    Tries right, left, above and below each point; takes the first spot clear of the
    other labels, the dots and the plot edges. If none is clear, the label goes below
    the lowest clear spot on the right with a leader line back to its point.
    Returns (text, x, y, anchor, leader start or None).
    """
    taken: list[Rect] = []
    out = []
    rect = _rect

    def clear(r: Rect) -> bool:
        return _clear(r, bounds, taken + obstacles)

    for text, cx, cy in sorted(points, key=lambda p: (p[2], p[1])):
        w = len(text) * CHAR_W
        spots = [
            (cx + 10, cy + 4, "start"),
            (cx - 10, cy + 4, "end"),
            (cx, cy - 12, "middle"),
            (cx, cy + 20, "middle"),
            (cx + 8, cy - 10, "start"),
            (cx + 8, cy + 18, "start"),
            (cx - 8, cy - 10, "end"),
            (cx - 8, cy + 18, "end"),
        ]
        for lx, ly, anchor in spots:
            r = rect(lx, ly, w, anchor)
            if clear(r):
                taken.append(r)
                out.append((text, lx, ly, anchor, None))
                break
        else:
            # Search outward, above and below, on both sides; draw a leader line.
            for k in range(1, 16):
                hit = None
                for dy in (-14 * k - 4, 14 * k + 4):
                    for dx, anchor in ((18, "start"), (-18, "end")):
                        lx, ly = cx + dx, cy + dy
                        r = rect(lx, ly, w, anchor)
                        if clear(r):
                            hit = (lx, ly, anchor, r)
                            break
                    if hit:
                        break
                if hit:
                    lx, ly, anchor, r = hit
                    taken.append(r)
                    out.append((text, lx, ly, anchor, (cx, cy)))
                    break
            else:
                out.append((text, cx + 10, cy + 4, "start", (cx, cy)))
    return out


#: Model families, in their fixed color order. A value whose label names none of them is
#: in "other". Each family has a color (STYLE) and a marker shape, so a reader who
#: cannot tell the colors apart still can tell the families apart.
FAMILIES = (("nova", "Amazon Nova"), ("claude", "Claude"))
FAMILY_ORDER = ("nova", "claude", "other")
SHAPES = {"nova": "circle", "claude": "square", "other": "diamond"}
#: Space between two panels side by side, and the title line above each one.
COL_GAP = 16
PANEL_HEAD_H = 24
KEY_LINE_H = 16
CONTEXT_R = 3.5

#: A scatter point: (value, x {"mean", "se"}, {metric key: y {"mean", "se"}}).
Point = tuple[str, dict[str, float], dict[str, dict[str, float]]]
#: A dataset's points: (title, models line, points, {metric key: (baseline y, name)}).
Group = tuple[str, Models, list[Point], dict[str, tuple[float, str]]]
#: A plotted metric: (key, name, short name for an axis or a key entry).
YMetric = tuple[str, str, str]


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


def _y_ticks(ys: list[float], means: list[float]) -> tuple[list[float], int]:
    """Ticks covering ys, stopping at 1.0 when no mean passes it (a score cannot)."""
    yt = _span(ys, allow_negative=min(ys) < 0)
    if max(means) <= 1.0:
        yt = [t for t in yt if t <= 1.0 + 1e-9]
        if yt[-1] < 1.0 and max(ys) > yt[-1]:
            yt.append(1.0)
    return yt, tick_decimals(yt)


def _scatter_panel(
    *,
    width: float,
    head: str,
    xt: list[float],
    x_ticks: list[str],
    yt: list[float],
    x_name: str,
    y_name: str,
    y_label: str,
    own: list[tuple[str, dict[str, float], dict[str, float], str]],
    context: list[tuple[str, dict[str, float], dict[str, float]]],
    lines: list[tuple[str, str, str, bool]],
    labelled: set[str],
    label_of: Any,
    x_exact: Any,
    baseline: tuple[float, str] | None,
    note: bool,
    numbers: dict[str, int] | None,
    colored: bool,
) -> tuple[str, bool]:
    """One scatter panel in its own coordinates; return the SVG and whether it is clean.

    own: the panel's points (value, x, y, family); context: other families' points,
    drawn as small gray dots; lines: (text, from value, to value, measurable) for the
    marginal cost of error. Clean means every label found a spot by its dot and every
    line label a spot on its line; numbers, when given, replace the labels.
    """
    left, right = SC_LEFT, width - 24
    y0 = PANEL_HEAD_H if head else 0
    y1 = y0 + SC_PLOT_H

    def px(v: float) -> float:
        return left + (v - xt[0]) / (xt[-1] - xt[0]) * (right - left)

    def py(v: float) -> float:
        return y1 - (v - yt[0]) / (yt[-1] - yt[0]) * (y1 - y0)

    y_dec = tick_decimals(yt)
    body: list[str] = [head]
    for t in yt:
        body.append(
            f'<line class="grid" x1="{left}" x2="{right}" '
            f'y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
            f'<text class="tick" x="{left - 8}" y="{py(t) + 4:.1f}" '
            f'text-anchor="end">{t:.{y_dec}f}</text>'
        )
    for t, text in zip(xt, x_ticks):
        body.append(
            f'<text class="tick" x="{px(t):.1f}" y="{y1 + 16}" '
            f'text-anchor="middle">{text}</text>'
        )
    body.append(
        f'<line class="axis" x1="{left}" x2="{right}" y1="{y1}" y2="{y1}"/>'
        f'<line class="axis" x1="{left}" x2="{left}" y1="{y0}" y2="{y1}"/>'
        f'<text class="axislabel" x="{(left + right) / 2:.1f}" '
        f'y="{y1 + 34}" text-anchor="middle">{escape(x_name)}</text>'
        f'<text class="axislabel" transform="translate(12 {(y0 + y1) / 2:.1f}) '
        f'rotate(-90)" text-anchor="middle">{escape(y_label)}</text>'
    )
    obstacles: list[Rect] = []
    if note:
        obstacles.append((left + 6, y0 + 2, left + 6 + NOTE_W, y0 + 18))
    tail: list[str] = []
    if baseline is not None:
        value, name = baseline
        by = py(value)
        w = len(name) * CHAR_W * 0.92
        body.append(
            f"<g><title>{escape(name)}: {fmt(value)}</title>"
            f'<line class="baseline" x1="{left}" x2="{right}" '
            f'y1="{by:.1f}" y2="{by:.1f}"/></g>'
        )
        obstacles.append(_rect(right - 4, by - 4, w, "end"))
        tail.append(
            f'<text class="baselinelabel halo" x="{right - 4:.1f}" y="{by - 4:.1f}" '
            f'text-anchor="end">{escape(name)}</text>'
        )
    centre = {v: (px(x["mean"]), py(y["mean"])) for v, x, y, _ in own}
    centre.update({v: (px(x["mean"]), py(y["mean"])) for v, x, y in context})

    # Bands first, so the dots and the cost-of-error lines sit on top.
    for value, _, y, fam in own:
        cx, cy = centre[value]
        dy = 2 * y.get("se", 0)
        band_top, band_bottom = py(y["mean"] + dy), py(y["mean"] - dy)
        obstacles.append((cx - 7, cy - 7, cx + 7, cy + 7))
        obstacles.append((cx - BAND, band_top, cx + BAND, band_bottom))
        if band_bottom > band_top:
            cls = f"band fam-{fam}" if colored else "band"
            body.append(
                f'<rect class="{cls}" x="{cx - BAND:.1f}" y="{band_top:.1f}" '
                f'width="{2 * BAND}" height="{band_bottom - band_top:.1f}" '
                f'rx="{BAND}"/>'
            )
    for value, x, y in context:
        cx, cy = centre[value]
        obstacles.append((cx - 5, cy - 5, cx + 5, cy + 5))
        body.append(
            f"<g><title>{escape(value)}: {escape(y_name)} {fmt(y['mean'])}, "
            f"{escape(x_name)} {x_exact(x['mean'])}</title>"
            + marker(SHAPES[family(value)], cx, cy, CONTEXT_R, "ctx")
            + "</g>"
        )
    segments: list[tuple[str, float, float, float, float]] = []
    for text, ref, value, measurable in lines:
        (ax, ay), (bx, by) = centre[ref], centre[value]
        segments.append((text, ax, ay, bx, by))
        body.append(
            f"<g><title>{escape(value)} against {escape(ref)}: {text}</title>"
            f'<line class="mcoe{"" if measurable else " noise"}" '
            f'x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}"/></g>'
        )
    for value, x, y, fam in own:
        cx, cy = centre[value]
        cls = f"dot fam-{fam}" if colored else "dot"
        body.append(
            f"<g><title>{escape(value)}: {escape(y_name)} {fmt(y['mean'])} ± "
            f"{fmt(2 * y.get('se', 0))}, {escape(x_name)} {x_exact(x['mean'])}</title>"
            + marker(SHAPES[fam] if colored else "circle", cx, cy, 5, cls)
            + "</g>"
        )
    if note:
        # Drawn after the lines and dots, so its halo keeps them off the words.
        body.append(
            f'<text class="note halo" x="{left + 8}" y="{y0 + 14}">'
            "Better: up and to the left</text>"
        )
    # Point labels and dot numbers also keep off the cost-of-error lines.
    line_boxes = [
        (
            ax + t * (bx - ax) - 1,
            ay + t * (by - ay) - 1,
            ax + t * (bx - ax) + 1,
            ay + t * (by - ay) + 1,
        )
        for _, ax, ay, bx, by in segments
        for n in [max(2, int(math.hypot(bx - ax, by - ay) / 6))]
        for t in (i / n for i in range(1, n))
    ]
    bounds = (left + 2, y0 + 20, right, y1 - 2)
    screen = [
        (label_of(value, x, y), *centre[value])
        for value, x, y, _ in own
        if value in labelled
    ]
    placed = _place_labels(screen, bounds, obstacles + line_boxes)
    blocked = obstacles + [
        _rect(lx, ly, len(text) * CHAR_W, anchor) for text, lx, ly, anchor, _ in placed
    ]
    on_lines = _place_on_lines(segments, bounds, blocked)
    clean = not any(leader for *_, leader in placed) and len(on_lines) == len(segments)
    if numbers is not None:
        marks = _place_labels(
            [(str(numbers[v]), *centre[v]) for v, _, _, _ in own],
            (left + 2, y0, right, y1 - 2),
            obstacles + line_boxes,
        )
        for text, lx, ly, anchor, leader in marks:
            if leader:
                body.append(
                    f'<line class="axis" x1="{leader[0]:.1f}" y1="{leader[1]:.1f}" '
                    f'x2="{lx:.1f}" y2="{ly - 4:.1f}"/>'
                )
            body.append(
                f'<text class="num halo" x="{lx:.1f}" y="{ly:.1f}" '
                f'text-anchor="{anchor}">{text}</text>'
            )
    else:
        for text, lx, ly, anchor, _ in placed:
            body.append(
                f'<text class="label halo" x="{lx:.1f}" y="{ly:.1f}" '
                f'text-anchor="{anchor}">{escape(text)}</text>'
            )
        for text, lx, ly in on_lines:
            body.append(
                f'<text class="mcoelabel halo" x="{lx:.1f}" y="{ly:.1f}" '
                f'text-anchor="middle">{escape(text)}</text>'
            )
    return "".join(body + tail), clean


#: Characters per key line: 12px text averages about 6.2px a character.
KEY_CHARS = int((WIDTH - SC_LEFT) / 6.2) - 2


def _key(
    points: list[Point],
    numbers: dict[str, int],
    y_axes: dict[str, tuple[list[float], int]],
    names: dict[str, str],
    shorts: dict[str, str],
    gains_by: dict[str, tuple[str, dict[str, float | None]]],
    x_name: str,
    x_text: Any,
    bottom: float,
) -> tuple[str, float]:
    """The key under numbered charts: a header line, then one entry per value.

    With a marginal cost of error, the header names the cheapest value and every
    entry says how it compares with it; otherwise the header says what each entry
    holds. Entries too long for the width continue on an indented line.
    """
    multi = len(y_axes) > 1
    measured = next(((k, g) for k, g in gains_by.items() if g[1]), None)

    def acc(k: str, y: dict[str, float]) -> str:
        return f"{y['mean']:.{max(2, y_axes[k][1])}f}"

    def values(yy: dict[str, dict[str, float]], first: str | None) -> str:
        order = ([first] if first else []) + [k for k in y_axes if k != first]
        return ", ".join(
            (f"{shorts[k]} " if multi else "") + acc(k, yy[k]) for k in order if k in yy
        )

    by_value = {v: (x, yy) for v, x, yy in points}
    if measured:
        metric, (ref, gains) = measured
        rx, ryy = by_value[ref]
        head = (
            f"Compared with {ref}, the cheapest ({acc(metric, ryy[metric])}, "
            f"{x_text(rx['mean'])}). One error = one document's worth of wrong fields."
        )
    else:
        metric, ref, gains = None, "", {}
        y_part = ", ".join((shorts if multi else names)[k] for k in y_axes)
        head = f"Key. Each line: value: {y_part}, {lower_name(x_name)}."
    out: list[str] = []
    y = bottom + 14
    for line in textwrap.wrap(head, int((WIDTH - SC_LEFT) / SUB_CHAR_W) - 2):
        out.append(
            f'<text class="keyhead" x="{SC_LEFT}" y="{y:.1f}">{escape(line)}</text>'
        )
        y += SUB_LINE_H
    y += KEY_LINE_H - SUB_LINE_H
    for value, x, yy in points:
        entry = f"{value}: {values(yy, metric)}, {x_text(x['mean'])}"
        if metric and value == ref:
            entry += " (cheapest)"
        elif metric and value in gains:
            cost = gains[value]
            if cost is None:
                entry += f" — no measurable gain over {ref}"
            else:
                rx, ryy = by_value[ref]
                fewer = round(1000 * (yy[metric]["mean"] - ryy[metric]["mean"]))
                entry += (
                    f" — +{money(x['mean'] - rx['mean'])} per 1,000 documents for "
                    f"{fewer:,} fewer errors per 1,000: {mcoe_text(cost)}"
                )
        n = numbers[value]
        lines = textwrap.wrap(entry, KEY_CHARS - len(str(n)) - 2)
        out.append(
            f'<text class="label" x="{SC_LEFT}" y="{y:.1f}">'
            f'<tspan class="num">{n}</tspan>  {escape(lines[0])}</text>'
        )
        for more in lines[1:]:
            y += KEY_LINE_H
            out.append(
                f'<text class="label" x="{SC_LEFT + 18}" y="{y:.1f}">'
                f"{escape(more)}</text>"
            )
        y += KEY_LINE_H
    return "".join(out), y - bottom - KEY_LINE_H + 14


def lower_name(name: str) -> str:
    """Lower-case a metric name mid-sentence, leaving an acronym such as AUROC alone."""
    return name if name[:2].isupper() else name[:1].lower() + name[1:]


def draw_scatter(
    x_name: str,
    ys: list[YMetric],
    groups: list[Group],
    runs: int,
    *,
    x_money: bool = False,
    mcoe: frozenset[str] | set[str] = frozenset(),
    prices: str = "",
    families: bool = False,
) -> str:
    """Return an SVG document plotting each y metric against x (cost or tokens).

    ys: the metrics on the y-axis, one chart each, side by side.
    groups: one per dataset, [(dataset name, models line, points, baselines)]; a
        baseline is drawn as a dashed line across that metric's chart.
    x_money: x is cost per 1,000 documents in US dollars (otherwise tokens per page).
    mcoe: the y metrics that get marginal-cost-of-error lines from the cheapest value.
    prices: a price-list note for a chart that shows cost, or "" for one that does not.
    families: color and shape each point by model family, and when a dataset has
        more than one family, give each family its own chart on the same axes, with
        the other families' points as gray context dots.

    Every chart sits in its own <g transform>, so the SVG keeps one <style> and one
    root that the docs site can inline. A dataset whose labels cannot all sit by
    their dots has its dots numbered, with a key under its charts.
    """

    def x_text(v: float) -> str:
        return money(v) if x_money else _compact(v, 10_000)

    def x_exact(v: float) -> str:
        return money(v) if x_money else fmt(v)

    names = {k: name for k, name, _ in ys}
    shorts = {k: short for k, _, short in ys}
    body: list[str] = []
    desc_parts: list[str] = []
    top = TOP
    for ds_name, models, points, baselines in groups:
        body.append(
            f'<text class="panel" x="0" y="{top + 18}">{escape(ds_name)}</text>'
        )
        sub, sub_h = models_svg(models, top + 18)
        body.append(sub)
        start = top + PANEL_TITLE_H + sub_h
        present = [f for f in FAMILY_ORDER if any(family(v) == f for v, _, _ in points)]
        split_families = families and len(present) > 1
        specs = [
            (key, fam)
            for key, _, _ in ys
            for fam in (present if split_families else [None])
            if any(
                key in yy and (fam is None or family(v) == fam) for v, _, yy in points
            )
        ]
        cols = 1 if len(specs) == 1 else 2
        width = WIDTH if cols == 1 else (WIDTH - COL_GAP) / 2
        panel_h = (PANEL_HEAD_H if len(specs) > 1 else 0) + SC_PLOT_H + SC_BOTTOM
        # Each dataset gets its own ranges: results on different datasets are not
        # compared with each other, and a shared range squeezes the small ones. Its
        # charts share the x range, and the charts of one metric share the y range.
        xt = _span(
            [p[1]["mean"] for p in points],
            min_span=0.20,
            pad=0.15,
            count=5 if cols == 1 else 4,
            allow_negative=False,
        )
        x_ticks = _money_ticks(xt) if x_money else [_compact(t, xt[-1]) for t in xt]
        y_axes: dict[str, tuple[list[float], int]] = {}
        gains_by: dict[str, tuple[str, dict[str, float | None]]] = {}
        for key, _, _ in ys:
            pts = [(v, x, yy[key]) for v, x, yy in points if key in yy]
            if not pts:
                continue
            vals = [
                y["mean"] + s * 2 * y.get("se", 0) for *_, y in pts for s in (-1, 1)
            ]
            if key in baselines:
                vals.append(baselines[key][0])
            y_axes[key] = _y_ticks(vals, [y["mean"] for *_, y in pts])
            if key in mcoe:
                gains_by[key] = marginal_cost_of_error(pts)
                ref, gains = gains_by[key]
                if gains:
                    desc_parts.append(
                        f"{ds_name}, marginal cost of error against {ref}: "
                        + ", ".join(f"{v} {mcoe_text(c)}" for v, c in gains.items())
                    )
        numbers = {v: i for i, (v, _, _) in enumerate(points, 1)}

        def render(numbered: bool) -> tuple[list[str], bool]:
            out: list[str] = []
            clean = True
            for i, (key, fam) in enumerate(specs):
                yt, y_dec = y_axes[key]
                pts = [(v, x, yy[key]) for v, x, yy in points if key in yy]
                own = [
                    (v, x, y, family(v)) for v, x, y in pts if fam in (None, family(v))
                ]
                context = [(v, x, y) for v, x, y in pts if fam not in (None, family(v))]
                ref, gains = gains_by.get(key, ("", {}))
                own_values = {v for v, *_ in own}
                lines = [
                    (mcoe_text(c, short=True), ref, v, c is not None)
                    for v, c in gains.items()
                    if v in own_values
                ]
                labelled = (
                    own_values
                    if len(own) <= MAX_LABELLED
                    else (_frontier(pts) | {v for v, *_ in pts if "(default)" in v})
                    & own_values
                )

                def label_of(
                    value: str, x: dict[str, float], y: dict[str, float], d: int = y_dec
                ) -> str:
                    return f"{value}: {y['mean']:.{max(2, d)}f}, {x_text(x['mean'])}"

                head = ""
                if len(specs) > 1:
                    parts = [names[key]] if len(ys) > 1 else []
                    glyph = ""
                    if fam is not None:
                        parts.append(family_title(fam, [v for v, *_ in own]))
                        glyph = marker(SHAPES[fam], 6, 9, 5, f"dot fam-{fam}")
                    head = glyph + (
                        f'<text class="ptitle" x="{16 if glyph else 0}" y="13">'
                        f"{escape(' · '.join(parts))}</text>"
                    )
                svg, ok = _scatter_panel(
                    width=width,
                    head=head,
                    xt=xt,
                    x_ticks=x_ticks,
                    yt=yt,
                    x_name=x_name,
                    y_name=names[key],
                    y_label=names[key] if len(ys) == 1 else shorts[key],
                    own=own,
                    context=context,
                    lines=lines,
                    labelled=labelled,
                    label_of=label_of,
                    x_exact=x_exact,
                    baseline=baselines.get(key),
                    note=i == 0,
                    numbers=numbers if numbered else None,
                    colored=families,
                )
                clean = clean and ok
                ox = (i % cols) * (width + COL_GAP)
                oy = start + (i // cols) * panel_h
                out.append(f'<g transform="translate({ox:.1f} {oy:.1f})">{svg}</g>')
            return out, clean

        panels_svg, clean = render(numbered=False)
        if not clean:
            panels_svg, _ = render(numbered=True)
        body += panels_svg
        bottom = start + math.ceil(len(specs) / cols) * panel_h
        key_h = 0
        if not clean:
            key_svg, key_h = _key(
                points, numbers, y_axes, names, shorts, gains_by, x_name, x_text, bottom
            )
            body.append(key_svg)
        for key, (value, name) in baselines.items():
            if key in y_axes:
                desc_parts.append(f"{ds_name}, {names[key]}, {name}: {fmt(value)}")
        top = bottom + key_h + PANEL_GAP
    height = top - PANEL_GAP + (PRICES_H if prices else 0)
    body.append(prices_svg(prices, height - 5))

    desc = "; ".join(
        [
            f"{ds}: "
            + "; ".join(
                (f"{names[k]}: " if len(ys) > 1 else "")
                + ", ".join(
                    f"{v} {fmt(yy[k]['mean'])} at {x_exact(x['mean'])}"
                    for v, x, yy in rows
                    if k in yy
                )
                for k, _, _ in ys
                if any(k in yy for _, _, yy in rows)
            )
            for ds, _, rows, _ in groups
        ]
        + desc_parts
    )
    y_title = " and ".join(
        n if i == 0 else lower_name(n) for i, (_, n, _) in enumerate(ys)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" width="{WIDTH}" '
        f'height="{height}" viewBox="0 0 {WIDTH} {height}" role="img">'
        f"<title>{escape(y_title)} against {escape(x_name)}, mean of {runs} runs "
        f"with two standard errors either side</title><desc>{escape(desc)}</desc>"
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
    x_name: str,
    y_name: str,
    series: list[tuple[str, list[tuple[str, float, float]]]],
    *,
    title: str = "",
    models: Models = "",
    slots: Slots | None = None,
) -> str:
    """Return an SVG plotting one per-document score against the document's size.

    series: one entry per value of the setting, [(value, [(document, x, y), ...])].
        x is a count, drawn on a log axis; y is a score. One point per document per
        value, colored and shaped by value, with the running median of each value
        over log-spaced bins as a line, so a trend reads without a fitted curve.
    slots: optional (color, shape) per series, for more series than there are
        colors; by default each series takes the next color and shape.
    """
    styles = _slots(len(series), slots)
    points = [(x, y) for _, pts in series for _, x, y in pts]
    if not points:
        return ""
    xt = _decades(min(x for x, _ in points), max(x for x, _ in points))
    ys = [y for _, y in points]
    yt = (
        [0.0, 0.25, 0.5, 0.75, 1.0]
        if min(ys) >= 0 and max(ys) <= 1
        else _span(ys, pad=0.08)
    )
    y_dec = tick_decimals(yt)
    left, right = SC_LEFT, WIDTH - 24
    body: list[str] = []
    top = TOP
    if title:
        body.append(
            f'<text class="panel" x="0" y="{top + 18:.1f}">{escape(title)}</text>'
        )
        sub, sub_h = models_svg(models, top + 18)
        body.append(sub)
        top += PANEL_TITLE_H + sub_h
    key_svg, key_h = _legend(
        [v for v, _ in series], top + 10, left, right - left, slots
    )
    body.append(key_svg)
    y0 = top + key_h + 6
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
            f'text-anchor="end">{t:.{y_dec}f}</text>'
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
        f'<text class="axislabel" x="{(left + right) / 2:.1f}" y="{y1 + 34}" '
        f'text-anchor="middle">{escape(x_name)}</text>'
        f'<text class="axislabel" transform="translate(12 {(y0 + y1) / 2:.1f}) '
        f'rotate(-90)" text-anchor="middle">{escape(y_name)}</text>'
    )
    desc_parts: list[str] = []
    for (value, pts), (c, sh) in zip(series, styles, strict=True):
        for doc, x, y in pts:
            body.append(
                f"<g><title>{escape(doc)}: {escape(value)}, {x:,.0f} rows, "
                f"{escape(lower_name(y_name))} {y:.2f}</title>"
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
            + ", ".join(f"{y:.2f} at {x:,.0f} rows" for x, y in trend)
        )
    height = y1 + SC_BOTTOM
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" width="{WIDTH}" '
        f'height="{height:.0f}" viewBox="0 0 {WIDTH} {height:.0f}" role="img">'
        f"<title>{escape(y_name)} against {escape(lower_name(x_name))}, one point "
        f"per document per value</title><desc>{escape('; '.join(desc_parts))}</desc>"
        f"<style>{STYLE}{SERIES_STYLE}</style>" + "".join(body) + "</svg>\n"
    )
