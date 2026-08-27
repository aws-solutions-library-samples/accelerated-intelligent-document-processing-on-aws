#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render a per-flow usability report card: verdict, metrics, and a stage-by-stage
filmstrip of what the agent did.

Each stage shows TWO things side by side, and the distinction matters:

  * the SCREENSHOT — for the human reviewer. The agent never saw this.
  * "what the agent saw" — the literal tool result the model consumed, normally an
    accessibility tree. This is the agent's actual evidence.

Showing only the screenshot would misrepresent the run: a control that is visually
obvious but has no accessible name is invisible to the agent, and the filmstrip has to
make that legible rather than hide it.

No external CSS/JS. Self-contained HTML next to the PNGs so the whole directory can be
zipped and attached to a ticket.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

VERDICT_STYLE = {
    True: ("PASS", "#116329", "#dafbe1"),
    False: ("FAIL", "#82071e", "#ffebe9"),
    None: ("UNVERIFIED", "#7d4e00", "#fff8c5"),
}

CSS = """
*{box-sizing:border-box}
body{margin:0;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
     color:#1f2328;background:#f6f8fa}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:32px 0 12px;padding-bottom:6px;border-bottom:1px solid #d1d9e0}
.sub{color:#59636e;margin:0 0 20px}
.badge{display:inline-block;padding:3px 10px;border-radius:2em;font-weight:600;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.kpi{background:#fff;border:1px solid #d1d9e0;border-radius:6px;padding:12px}
.kpi .n{font-size:22px;font-weight:600}
.kpi .l{color:#59636e;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.card{background:#fff;border:1px solid #d1d9e0;border-radius:6px;margin:0 0 14px;overflow:hidden}
.card>summary{cursor:pointer;padding:10px 14px;display:flex;gap:10px;align-items:center;
              font-weight:600;list-style:none}
.card>summary::-webkit-details-marker{display:none}
.card>summary:hover{background:#f6f8fa}
.idx{background:#eaeef2;border-radius:4px;padding:1px 7px;font-variant-numeric:tabular-nums;
     color:#59636e;font-weight:600}
.tool{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
      background:#eaeef2;border-radius:4px;padding:1px 6px}
.meta{margin-left:auto;color:#59636e;font-weight:400;font-size:12px}
.body{border-top:1px solid #d1d9e0;padding:14px;display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.body{grid-template-columns:1fr}}
.pane h4{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:#59636e}
img{width:100%;border:1px solid #d1d9e0;border-radius:4px;display:block}
pre{margin:0;background:#f6f8fa;border:1px solid #d1d9e0;border-radius:4px;padding:10px;
    max-height:420px;overflow:auto;font-size:11.5px;line-height:1.45;white-space:pre-wrap}
.url{font-family:ui-monospace,monospace;font-size:11.5px;color:#59636e;word-break:break-all;
     padding:0 14px 10px}
.finding{background:#fff;border:1px solid #d1d9e0;border-left:3px solid #bf8700;
         border-radius:6px;padding:12px 14px;margin:0 0 10px}
.finding.bug{border-left-color:#cf222e}
.finding h4{margin:0 0 6px;font-size:13px}
.finding .q{color:#59636e}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d1d9e0;border-radius:6px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #d1d9e0;font-size:13px;vertical-align:top}
th{background:#f6f8fa;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:#59636e}
tr:last-child td{border-bottom:none}
.note{background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;padding:10px 14px;
      margin:14px 0;font-size:13px}
.err{color:#cf222e;font-weight:600}
"""


def _esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""))


def _kpi(label: str, value: Any, hint: str = "") -> str:
    h = f'<div class="l" style="margin-top:4px">{_esc(hint)}</div>' if hint else ""
    return f'<div class="kpi"><div class="l">{_esc(label)}</div><div class="n">{_esc(value)}</div>{h}</div>'


def render(result: dict, out_dir: Path) -> Path:
    """Write report.html into out_dir (alongside the stage PNGs). Returns its path."""
    rep = result.get("report") or {}
    ver = result.get("verification") or {}
    meas = result.get("measured") or {}
    stages = result.get("stages") or []

    label, fg, bg = VERDICT_STYLE.get(ver.get("confirmed"), VERDICT_STYLE[None])
    flow = result.get("flow_id", "?")
    mode = result.get("mode", "?")

    doc_steps = result.get("documented_steps")
    gap = ""
    if doc_steps:
        # The headline metric: a feature can pass every assertion and still cost 4x the
        # documented effort. That gap is the failure mode this whole tier exists to find.
        gap = f"{meas.get('clicks', 0)} clicks vs {doc_steps} documented steps"

    p: list[str] = []
    p.append("<!doctype html><meta charset=utf-8>")
    p.append(f"<title>UAT report card — {_esc(flow)} ({_esc(mode)})</title>")
    p.append(f"<style>{CSS}</style><div class=wrap>")
    p.append(f"<h1>{_esc(flow)} <span class=badge style='background:{bg};color:{fg}'>{label}</span></h1>")
    p.append(
        f"<p class=sub><b>{_esc(mode.upper())}</b> run &middot; "
        + ("no documentation given — measuring discoverability" if mode == "cold"
           else "documented procedure given — measuring documentation accuracy")
        + "</p>"
    )

    # ---- KPIs -------------------------------------------------------------
    p.append('<div class=grid>')
    p.append(_kpi("Verified", label, ver.get("method", "")))
    p.append(_kpi("Agent difficulty", rep.get("difficulty", "?"), "self-reported"))
    p.append(_kpi("Clicks", meas.get("clicks", 0), "instrumented"))
    p.append(_kpi("Tool calls", meas.get("tool_calls", 0), "effort proxy"))
    p.append(_kpi("Confusions", len(rep.get("confusions") or []), "agent-reported"))
    p.append(_kpi("Page errors", len(meas.get("page_errors") or []), "any > 0 is a defect"))
    p.append("</div>")

    if gap:
        p.append(f"<div class=note><b>Complexity gap:</b> {_esc(gap)}.</div>")
    if meas.get("collect_error"):
        p.append(f"<div class=note><span class=err>Instrumentation problem:</span> "
                 f"{_esc(meas['collect_error'])} — click counts below may be understated.</div>")
    if result.get("hit_tool_cap"):
        p.append("<div class=note><b>Abandoned:</b> the agent exhausted its tool-call "
                 "budget. Treat this as a finding, not an error.</div>")
    if result.get("agent_error"):
        p.append(f"<div class=note><span class=err>Agent error:</span> {_esc(result['agent_error'])}</div>")

    # ---- verification -----------------------------------------------------
    p.append("<h2>External verification</h2>")
    p.append("<table><tr><th>Method</th><th>Confirmed</th><th>Evidence</th></tr>")
    p.append(f"<tr><td><span class=tool>{_esc(ver.get('method'))}</span></td>"
             f"<td>{_esc(ver.get('confirmed'))}</td><td>{_esc(ver.get('evidence'))}</td></tr></table>")
    p.append("<p class=sub style='margin-top:8px'>The agent does not decide this. "
             "Confirmation is deterministic Python reading the system of record.</p>")
    detail = ver.get("detail")
    if isinstance(detail, dict):
        p.append(f"<pre>{_esc(json.dumps(detail, indent=2))}</pre>")

    # ---- findings ---------------------------------------------------------
    confusions = rep.get("confusions") or []
    mismatches = rep.get("docs_mismatch") or []
    dead_ends = rep.get("dead_ends") or []
    if confusions or mismatches or dead_ends:
        p.append("<h2>Findings</h2>")
        for c in confusions:
            p.append('<div class="finding bug"><h4>Confusion</h4>')
            p.append(f"<div class=q>at <span class=tool>{_esc(c.get('where'))}</span></div>")
            p.append(f"<p><b>Unclear:</b> {_esc(c.get('what_was_unclear'))}</p>")
            p.append(f"<p><b>Expected:</b> {_esc(c.get('what_i_expected'))}</p></div>")
        for m in mismatches:
            p.append(f'<div class="finding bug"><h4>Documentation mismatch</h4><p>{_esc(m)}</p></div>')
        for d in dead_ends:
            p.append(f'<div class=finding><h4>Dead end</h4><p>{_esc(d)}</p></div>')

    # ---- filmstrip --------------------------------------------------------
    p.append(f"<h2>Stage-by-stage ({len(stages)} stages)</h2>")
    p.append("<p class=sub>Left: what a human would have seen. Right: the tool result the "
             "agent actually consumed. A control that is visually obvious but has no "
             "accessible name appears on the left and not the right — which is itself a finding.</p>")
    for st in stages:
        res = st.get("agent_saw") or ""
        bad = res.startswith(("ERROR", "NOT FOUND", "AMBIGUOUS"))
        args = f" <span class=q>{_esc(st.get('args'))}</span>" if st.get("args") else ""
        flag = ' <span class=err>&#9888;</span>' if bad else ""
        p.append("<details class=card>")
        p.append(f"<summary><span class=idx>{st.get('index')}</span>"
                 f"<span class=tool>{_esc(st.get('tool'))}</span>{args}{flag}"
                 f"<span class=meta>{st.get('clicks_so_far')} clicks &middot; "
                 f"{st.get('elapsed_ms')} ms</span></summary>")
        p.append(f"<div class=url>{_esc(st.get('url'))}</div>")
        p.append("<div class=body>")
        shot = st.get("screenshot")
        p.append('<div class=pane><h4>Screenshot (human view)</h4>'
                 + (f'<img src="{_esc(shot)}" loading=lazy alt="stage {st.get("index")}">'
                    if shot else "<pre>(no screenshot captured)</pre>")
                 + "</div>")
        p.append(f'<div class=pane><h4>What the agent saw</h4><pre>{_esc(res)}</pre></div>')
        p.append("</div></details>")

    # ---- narrative --------------------------------------------------------
    if rep.get("narrative"):
        p.append("<h2>Agent narrative</h2>")
        p.append(f"<pre>{_esc(rep['narrative'])}</pre>")
    if rep.get("claimed_action"):
        p.append("<h2>Claim submitted for verification</h2>")
        p.append(f"<pre>{_esc(rep['claimed_action'])}</pre>")

    p.append("</div>")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.html"
    path.write_text("\n".join(p), encoding="utf-8")
    return path
