#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render the SRT gate's verdict as a status badge (SVG).

Reads ``.srt/verdict.json``, which ``run.py`` writes once every check has run, and
writes a flat badge stating what the gate concluded. The ``srt_security_review``
CI job uploads it to the project's generic package registry on ``develop``, and a
project badge displays it.

The badge is published from the job itself, on pass and on failure, rather than
served from the job's artifacts: GitLab's "latest artifacts for a ref" URL only
reads the latest *successful* pipeline, so a failing gate would leave the last
green badge on display.

Usage: badge.py <verdict.json> <out.svg>
A missing or unreadable verdict renders as "no result", never as a pass.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

LABEL = "SRT"
GREEN, RED, ORANGE, GREY = "#2e7d32", "#c62828", "#ef6c00", "#757575"


def message_and_colour(verdict: dict | None) -> tuple[str, str]:
    if not verdict:
        return "no result", GREY
    outcome = verdict.get("outcome")
    if outcome == "pass":
        return "0 open high", GREEN
    if outcome == "open_high":
        count = int(verdict.get("open_high") or 0)
        return f"{count} open high", RED
    if outcome == "incomplete":
        return "scan incomplete", ORANGE
    if outcome == "stale_register":
        return "stale suppressions", ORANGE
    return "no result", GREY


def _width(text: str) -> int:
    # Approximates Verdana 11px, the face shields.io-style badges use.
    return 7 * len(text) + 10


def render(message: str, colour: str, label: str = LABEL) -> str:
    lw, mw = _width(label), _width(message)
    total = lw + mw
    label_x, msg_x = lw / 2, lw + mw / 2
    label, message = escape(label), escape(message)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" '
        f'role="img" aria-label="{label}: {message}">'
        f"<title>{label}: {message}</title>"
        f'<rect width="{lw}" height="20" fill="#555"/>'
        f'<rect x="{lw}" width="{mw}" height="20" fill="{colour}"/>'
        f'<g fill="#fff" text-anchor="middle" '
        f'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{label_x}" y="14">{label}</text>'
        f'<text x="{msg_x}" y="14">{message}</text>'
        f"</g></svg>\n"
    )


def load_verdict(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    message, colour = message_and_colour(load_verdict(Path(argv[1])))
    Path(argv[2]).write_text(render(message, colour), encoding="utf-8")
    print(f"SRT badge: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
