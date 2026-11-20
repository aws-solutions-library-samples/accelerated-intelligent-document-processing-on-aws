# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the guidance charts draw where the docs site's CSS is not there to help (EX-08).

The charts paint through CSS custom properties so the site's dark theme can swap the
palette, and a renderer without them (librsvg, behind every `rsvg-convert` preview)
drew every series black with no lines, error bars or rings. These pin the
presentation-attribute fallback, a by-size chart that shows each run (a list returned
whole or stopped short is two clusters, not one mean), a value that was never sent,
the per-value key of a scatter, and a subtitle and footnote that name OCR every value
holds in place of the baseline's.
"""

from __future__ import annotations

import re

from harness_import import harness_module

gp = harness_module("guidance_plots")

TABLES_LAYOUT = [{"name": "TABLES"}, {"name": "LAYOUT"}]


def attrs(svg: str, cls: str) -> list[str]:
    """The start tags whose class attribute is exactly `cls`."""
    return re.findall(rf'<\w+ class="{re.escape(cls)}"[^>]*>', svg)


def test_fallback_paint_is_the_light_themes_and_follows_specificity():
    svg = gp.paint_fallbacks(
        f"<svg><style>{gp.STYLE}{gp.SERIES_STYLE}</style>"
        '<circle class="dot s2" cx="1" cy="1" r="5"/>'
        '<circle class="dot ref" cx="1" cy="1" r="5"/>'
        '<text class="label s1" x="0" y="0">a</text>'
        '<rect class="gap" fill="#123456" x="0" y="0" width="1" height="1"/>'
        "</svg>"
    )
    # `.viz .s2` outranks `.dot`; the ring is `.dot`'s, the light theme's white.
    assert 'fill="#eb6834"' in attrs(svg, "dot s2")[0]
    assert 'stroke="#ffffff"' in attrs(svg, "dot s2")[0]
    # `.viz .dot.ref` outranks `.dot`, though it comes first in STYLE.
    assert 'stroke="#0b0b0b"' in attrs(svg, "dot ref")[0]
    assert 'fill="#2a78d6"' in attrs(svg, "label s1")[0]
    # An element that already says its paint keeps it.
    assert attrs(svg, "gap")[0].count("fill=") == 1
    # The dark theme's values are never the fallback.
    assert "#3987e5" not in svg.split("</style>")[1]


def test_a_by_size_chart_of_runs_draws_each_run_and_their_mean():
    series = [
        ("Single-pass", [("a", 400.0, 0.7), ("b", 400.0, 1.0)]),
        ("Agentic", [("a", 400.0, 1.0), ("b", 400.0, 1.0), ("a", 3200.0, 1.0)]),
    ]
    runs = [
        {"a": [1.0, 0.1075, 1.0], "b": [1.0, 1.0, 1.0]},
        {"a": [1.0, 1.0, 1.0], "b": [1.0, 1.0, 1.0]},
    ]
    svg = gp.draw_by_size(
        "Rows",
        "Rows that came back (%)",
        series,
        bands=[400, 401, 3200, 3201],
        runs=runs,
        not_sent=[(0, 3200.0)],
    )
    # One small point per document per run, in its value's color and shape.
    assert len(attrs(svg, "pt s1")) == 6
    assert "run 2: rows that came back (%) 11%" in svg
    # The large mark is the mean of the runs, as the results table's completeness
    # is: 5 of 6 runs whole and one at 11% is 85%, where a median would read 100%.
    assert "Single-pass, 400: mean rows that came back (%) 85% over 6 runs" in svg
    # Not sized by cost: no dot-area key and no price note.
    assert "Dot area" not in svg and "pricing.yaml" not in svg
    # Never sent at 3,200: written there in the series' color, not left a gap.
    assert ">not sent</text>" in svg
    assert re.search(r'<text class="label s1"[^>]*>Single-pass</text>', svg)


def test_without_runs_a_by_size_chart_is_as_it_was():
    series = [("On", [("a", 50.0, 0.5), ("b", 60.0, 0.7)])]
    svg = gp.draw_by_size("Rows", "Score", series, bands=[1, 100])
    assert not attrs(svg, "pt s1") and "runs of" not in svg


def test_ocr_every_value_holds_is_named_in_place_of_the_baselines():
    ab = {
        "baseline": "develop",
        "set": [
            {"extraction.mode": "simple", "ocr.features": TABLES_LAYOUT},
            {"extraction.mode": "advanced", "ocr.features": TABLES_LAYOUT},
        ],
    }
    base = gp._baseline(ab)
    assert gp._held_ocr(ab, base) == "Amazon Textract with Tables and Layout"
    subtitle, _, foot = gp._chart_texts("Varies: extraction mode", [], ab, "documents")
    assert subtitle.endswith(
        "Fixed: Amazon Textract with Tables and Layout; everything else: baseline setup"
    )
    assert foot.startswith(
        "Fixed: Amazon Textract with Tables and Layout, in place of the baseline "
        "setup's Amazon Textract with Layout only. Everything else is the baseline "
        "setup (the ablation registry's develop baseline): holistic classification"
    )
    # OCR the values vary, or that matches the baseline, is not "fixed".
    varied = {**ab, "set": [{"ocr.features": TABLES_LAYOUT}, {}]}
    assert gp._held_ocr(varied, base) == ""
    same = {**ab, "set": [{"ocr.features": [{"name": "LAYOUT"}]}]}
    assert gp._held_ocr(same, base) == ""
    plain = gp._chart_texts("Varies: x", [], {**ab, "set": [{}]}, "documents")
    assert plain[0].endswith("Everything else: baseline setup")


def test_a_scatter_key_names_every_value_and_marks_the_reference():
    svg, _ = gp._legend(
        ["Single-pass (baseline setup)", "Agentic, on"],
        20,
        0,
        700,
        [(0, 0), (1, 1)],
        lines=False,
        ref=0,
    )
    assert "swatch" not in svg
    assert 'class="dot s1 ref"' in svg and 'class="dot s2"' in svg
    assert re.search(r'class="plabel dflt"[^>]*>Single-pass \(baseline setup\)<', svg)
