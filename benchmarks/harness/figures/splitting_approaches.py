"""Emit the static splitting-approaches explainer SVG (no data; layout only)."""

import sys

OUT = sys.argv[1]

W = 792
PW = 376  # panel width
COLS = (0, W - PW)  # panel x offsets
TW, TH, TG = 30, 38, 6  # page tile width, height, gap
GG = 24  # gap between sections once split
DOCS = ["inv", "inv", "let", "frm", "frm", "frm"]
NAMES = {"inv": "Invoice", "let": "Letter", "frm": "Form"}
GROUPS = [(0, 2, "inv"), (2, 3, "let"), (3, 6, "frm")]  # [start, end) page index
BOX_H = 20
JOB_H = 30  # a box with a job line under the model name
CX = PW / 2  # diagram centre inside a panel

STYLE = """
  .viz { --ring: #ffffff; --ink: #0b0b0b; --ink-2: #52514e;
         --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7;
         --inv: #6250d6; --let: #eda100; --frm: #e87ba4; }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz, svg.viz:root {
      --ring: #17181c; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --axis: #383835;
      --inv: #9085e9; --let: #c98500; --frm: #d55181; }
  }
  :root[data-theme="dark"] .viz { --ring: #17181c; --ink: #f0efec;
    --ink-2: #c3c2b7; --muted: #898781; --grid: #2c2c2a; --axis: #383835;
    --inv: #9085e9; --let: #c98500; --frm: #d55181; }
  .viz { max-width: 100%; height: auto; }
  text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .viz .ctitle { font-size: 15px; font-weight: 600; fill: var(--ink); }
  .viz .sub { font-size: 12px; fill: var(--ink-2); }
  .viz .panel { font-size: 13px; font-weight: 600; fill: var(--ink); }
  .viz .lead { font-size: 12px; fill: var(--ink-2); }
  .viz .out { font-size: 12px; fill: var(--ink); }
  .viz .call { fill: var(--grid); }
  .viz .call.split { fill: var(--ring); stroke: var(--ink-2); stroke-width: 1;
    stroke-dasharray: 3 2; }
  .viz .call.both { stroke: var(--ink-2); stroke-width: 1; stroke-dasharray: 3 2; }
  .viz .calltext { font-size: 11px; font-weight: 600; fill: var(--ink); }
  .viz .jobtext { font-size: 10px; fill: var(--ink-2); }
  .viz .flow { stroke: var(--ink-2); stroke-width: 1; fill: none; }
  .viz .head { fill: var(--ink-2); }
  .viz .bracket { stroke: var(--ink-2); stroke-width: 1.25; fill: none; }
  .viz .inv { --doc: var(--inv); }
  .viz .let { --doc: var(--let); }
  .viz .frm { --doc: var(--frm); }
  .viz .page .sheet { fill: var(--doc); fill-opacity: 0.2; }
  .viz .page .ln { stroke: var(--doc); stroke-width: 2; stroke-linecap: round; }
  .viz .page.unread { opacity: 0.35; }
"""


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def page(x, y, doc, w=TW, h=TH, unread=False):
    cls = f"page {doc}" + (" unread" if unread else "")
    pad = w * 0.2
    lines = []
    for i, frac in enumerate((1.0, 1.0, 0.6)):
        ly = y + h * (0.24 + 0.17 * i)
        lines.append(
            f'<line class="ln" x1="{f(x + pad)}" x2="{f(x + pad + (w - 2 * pad) * frac)}"'
            f' y1="{f(ly)}" y2="{f(ly)}"/>'
        )
    return (
        f'<g class="{cls}"><rect class="sheet" x="{f(x)}" y="{f(y)}" width="{f(w)}"'
        f' height="{f(h)}" rx="3"/>' + "".join(lines) + "</g>"
    )


def page_xs(split):
    """Left x of each page, centred on CX; extra gap at section ends when split."""
    xs, x = [], 0.0
    for i in range(6):
        xs.append(x)
        x += TW + (GG if split and i in (1, 2) else TG)
    width = xs[-1] + TW
    return [CX - width / 2 + v for v in xs]


def arrow(x1, y1, x2, y2):
    return (
        f'<line class="flow" x1="{f(x1)}" y1="{f(y1)}" x2="{f(x2)}" y2="{f(y2)}"'
        ' marker-end="url(#ah)"/>'
    )


def call(cx, y, w, label_lines, h=BOX_H, kind="classify", job=None):
    """One model call. `kind` is its job: `split` finds where documents start
    (dashed outline), `classify` names a class (filled), `both` does both."""
    out = [
        f'<rect class="call {kind}" x="{f(cx - w / 2)}" y="{f(y)}" width="{f(w)}"'
        f' height="{f(h)}" rx="4"/>'
    ]
    lines = [(t, "calltext") for t in label_lines] + ([(job, "jobtext")] if job else [])
    n = len(lines)
    for i, (t, cls) in enumerate(lines):
        ty = y + h / 2 + 4 + (i - (n - 1) / 2) * 12
        out.append(
            f'<text class="{cls}" x="{f(cx)}" y="{f(ty)}"'
            f' text-anchor="middle">{t}</text>'
        )
    return "".join(out)


def brackets(xs, y):
    out = []
    for a, b, doc in GROUPS:
        x0, x1 = xs[a] + 1, xs[b - 1] + TW - 1
        out.append(
            f'<path class="bracket" d="M{f(x0)} {f(y - 5)}V{f(y)}H{f(x1)}V{f(y - 5)}"/>'
        )
        out.append(
            f'<text class="out" x="{f((x0 + x1) / 2)}" y="{f(y + 15)}"'
            f' text-anchor="middle">{NAMES[doc]}</text>'
        )
    return "".join(out)


def group_centre(xs, a, b):
    return (xs[a] + xs[b - 1] + TW) / 2


def spread(cx, n, step):
    return [cx + (i - (n - 1) / 2) * step for i in range(n)]


def captions(lines, y):
    return "".join(
        f'<text class="lead" x="0" y="{f(y + 16 * i)}">{t}</text>'
        for i, t in enumerate(lines)
    )


# Shared vertical rhythm (panel-local y)
T0 = 26  # top of the packet row
B1 = T0 + TH + 26  # top of the first call row
OUT1 = B1 + JOB_H + 26  # bracket line, one-step panels
T2 = B1 + JOB_H + 24  # top of the split packet row
B2 = T2 + TH + 26  # top of the second call row
DM_H = 32  # "Decision model" on two lines
OUT2 = B2 + DM_H + 24  # bracket line, two-step panels
CAP1 = OUT1 + 40
CAP2 = OUT2 + 40


def panel_page_by_page():
    xs = page_xs(False)
    g = [page(x, T0, d) for x, d in zip(xs, DOCS)]
    for x in xs:
        c = x + TW / 2
        g.append(arrow(c, T0 + TH + 3, c, B1 - 2))
        g.append(call(c, B1 + (JOB_H - BOX_H) / 2, TW + 2, ["LLM"]))
        g.append(arrow(c, B1 + (JOB_H + BOX_H) / 2 + 3, c, OUT1 - 3))
    g.append(
        f'<text class="jobtext" x="{f(xs[-1] + TW + 10)}" y="{f(B1 + JOB_H / 2 - 2)}">'
        "page</text>"
    )
    g.append(
        f'<text class="jobtext" x="{f(xs[-1] + TW + 10)}" y="{f(B1 + JOB_H / 2 + 10)}">'
        "classification</text>"
    )
    g.append(brackets(xs, OUT1))
    g.append(
        captions(
            [
                "One call per page: what class is this page?",
                "Pages in a row with the same class become one section.",
            ],
            CAP1,
        )
    )
    return "1. Page by page", g


def first_call(xs, g, w, kind, job):
    tops = spread(CX, 6, 11)
    for x, tx in zip(xs, tops):
        g.append(arrow(x + TW / 2, T0 + TH + 3, tx, B1 - 2))
    g.append(call(CX, B1, w, ["LLM"], h=JOB_H, kind=kind, job=job))


def panel_holistic():
    xs = page_xs(False)
    g = [page(x, T0, d) for x, d in zip(xs, DOCS)]
    first_call(xs, g, 176, "both", "section detection + classification")
    for (a, b, _), sx in zip(GROUPS, spread(CX, 3, 16)):
        g.append(arrow(sx, B1 + JOB_H + 3, group_centre(xs, a, b), OUT1 - 3))
    g.append(brackets(xs, OUT1))
    g.append(
        captions(
            [
                "One call reads the whole packet: which pages make up",
                "each document, and what class is each one?",
            ],
            CAP1,
        )
    )
    return "2. Holistic", g


def split_then(name_by):
    xs = page_xs(False)
    sx = page_xs(True)
    g = [page(x, T0, d) for x, d in zip(xs, DOCS)]
    first_call(xs, g, 104, "split", "section detection")
    for (a, b, _), ox in zip(GROUPS, spread(CX, 3, 16)):
        g.append(arrow(ox, B1 + JOB_H + 3, group_centre(sx, a, b), T2 - 3))
    for i, (x, d) in enumerate(zip(sx, DOCS)):
        first = any(i == a for a, _, _ in GROUPS)
        g.append(page(x, T2, d, unread=(name_by == "dm" and not first)))
    for a, b, _ in GROUPS:
        gc = group_centre(sx, a, b)
        if name_by == "llm":
            ends = spread(gc, b - a, 9)
            for i, ex in zip(range(a, b), ends):
                g.append(arrow(sx[i] + TW / 2, T2 + TH + 3, ex, B2 - 2))
            g.append(call(gc, B2 + (DM_H - BOX_H) / 2, 40, ["LLM"]))
            g.append(arrow(gc, B2 + (DM_H + BOX_H) / 2 + 3, gc, OUT2 - 3))
        else:
            g.append(arrow(sx[a] + TW / 2, T2 + TH + 3, gc, B2 - 2))
            g.append(call(gc, B2, 62, ["Decision", "model"], h=DM_H))
            g.append(arrow(gc, B2 + DM_H + 3, gc, OUT2 - 3))
    lx = group_centre(sx, *GROUPS[-1][:2]) + 31 + 8
    g.append(
        f'<text class="jobtext" x="{f(lx)}" y="{f(B2 + DM_H / 2 - 2)}">section</text>'
    )
    g.append(
        f'<text class="jobtext" x="{f(lx)}" y="{f(B2 + DM_H / 2 + 10)}">classification</text>'
    )
    g.append(brackets(sx, OUT2))
    return g


def panel_split_llm():
    g = split_then("llm")
    g.append(
        captions(
            [
                "First call: where does each document start?",
                "Then one call per section: what class is this section?",
            ],
            CAP2,
        )
    )
    return "3. Split, then an LLM classifies each section", g


def panel_split_dm():
    g = split_then("dm")
    g.append(
        captions(
            [
                "First call: where does each document start?",
                "Then a decision model looks at each section’s first page",
                "and picks its class from a fixed list.",
            ],
            CAP2,
        )
    )
    return "4. Split, then a decision model classifies each section", g


def build():
    body = []
    body.append(
        '<text class="ctitle" x="0" y="18">Four ways to find the documents in a'
        " packet and classify each one</text>"
    )
    # Key: what a page tile's colour means.
    ky = 32
    body.append(
        f'<text class="sub" x="0" y="{ky + 12}">Each tile is a page, colored by'
        " the document it belongs to:</text>"
    )
    kx = 338
    for doc, label_w in (("inv", 40), ("let", 34), ("frm", 30)):
        body.append(page(kx, ky + 1, doc, w=11, h=14))
        body.append(
            f'<text class="sub" x="{kx + 16}" y="{ky + 12}">{NAMES[doc]}</text>'
        )
        kx += 16 + label_w + 14
    ky2 = ky + 22
    body.append(
        f'<text class="sub" x="0" y="{ky2 + 12}">Each box is one model call:</text>'
    )
    bx = 172
    for kind, label, lw in (
        ("split", "section detection", 104),
        ("classify", "page or section classification", 178),
        ("both", "both in one call", 98),
    ):
        body.append(
            f'<rect class="call {kind}" x="{bx}" y="{ky2 + 1}" width="22"'
            ' height="14" rx="3"/>'
        )
        body.append(f'<text class="sub" x="{bx + 28}" y="{ky2 + 12}">{label}</text>')
        bx += 28 + lw + 16
    row1_y = 88
    row2_y = row1_y + CAP1 + 16 + 30
    panels = [
        (panel_page_by_page(), COLS[0], row1_y),
        (panel_holistic(), COLS[1], row1_y),
        (panel_split_llm(), COLS[0], row2_y),
        (panel_split_dm(), COLS[1], row2_y),
    ]
    for (title, g), px, py in panels:
        body.append(
            f'<g transform="translate({f(px)} {f(py)})">'
            f'<text class="panel" x="0" y="14">{title}</text>' + "".join(g) + "</g>"
        )
    height = row2_y + CAP2 + 32 + 10
    defs = (
        '<defs><marker id="ah" viewBox="0 0 6 6" refX="6" refY="3" markerWidth="6"'
        ' markerHeight="6" markerUnits="userSpaceOnUse" orient="auto">'
        '<path class="head" d="M0 0L6 3L0 6z"/></marker></defs>'
    )
    title = "Four ways to find the documents in a packet and classify each one"
    desc = (
        "The same six-page packet, holding an invoice (pages 1 and 2), a letter (page 3) "
        "and a form (pages 4 to 6), goes through four approaches, and each returns the "
        "same three sections. 1. Page by page: each page goes to an LLM on its own and "
        "gets a class; pages in a row with the same class become one section. "
        "2. Holistic: one LLM call reads the whole packet and returns the sections with "
        "their pages and classes. 3. Split, then an LLM classifies each section: one LLM call "
        "reads the whole packet and returns only where each document starts; then one LLM "
        "call per section classifies it. 4. Split, then a decision model classifies each "
        "section: the same first call finds the sections; then a decision model looks at "
        "each section's first page and picks its class from a fixed list."
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="viz" width="{W}" height="{f(height)}"'
        f' viewBox="0 0 {W} {f(height)}" role="img" aria-labelledby="t d">'
        f'<title id="t">{title}</title><desc id="d">{desc}</desc>'
        f"<style>{STYLE}</style>{defs}" + "".join(body) + "</svg>\n"
    )
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(OUT, W, height)


build()
