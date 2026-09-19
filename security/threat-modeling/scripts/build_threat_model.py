#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Build the machine-readable threat model export from the Markdown corpus.

The Markdown documents under ``security/threat-modeling/`` are the source of
truth for threat *content*. This script parses every ``| **Threat ID** |``
block out of them, joins it with the curated status/risk table below, and emits
``deliverables/threat-model.tc.json`` (Threat Composer shaped).

Why a script: the previous export drifted badly out of sync with the corpus
(58 threats in JSON vs 64 in Markdown, stopping at AUTH.T06), because it was
maintained by hand. Regenerate instead of editing the JSON:

    python3 security/threat-modeling/scripts/build_threat_model.py

``--check`` exits non-zero if the committed JSON differs from a fresh build, or
if the corpus and the STATUS table below have drifted apart. It runs in CI from
``make check-threat-model-currency`` (itself part of ``make lint-cicd``), which
also fails when the corpus falls more than one release behind ``VERSION``. It
was *not* gated before, and the export duly became unbuildable: AUTH.T13 was
added to the corpus with no STATUS entry, so every run exited on drift.

Export metadata (version, dates, the release the model was last reviewed
against) is read from ``README.md``'s Document Information table rather than
hardcoded here — see ``METADATA_ROWS``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deliverables" / "threat-model.tc.json"
INDEX = ROOT / "README.md"

# Documents whose threat blocks are authoritative. Everything else under
# threat-modeling/ (Talos reports, the historical security review, the AI
# brainstorm notes) is excluded so retired/duplicate ids don't leak in.
SKIP_MARKERS = ("Mitigation", "security-review", "ai-generated", "threat-id-glossary")

PREFIX_DOCS = {
    "PM": "Pipeline Mode",
    "BDA": "BDA Mode",
    "AGT": "Agent Analysis",
    "CHAT": "Companion Chat",
    "MCP": "MCP Integration",
    "KB": "Knowledge Base",
    "AUTH": "Authentication/RBAC",
    "SDK": "SDK/CLI",
    "HOOK": "Lambda Hooks",
    "UI": "Web UI",
    "RPT": "Reporting/Analytics",
    "FEAT": "Feature Platform",
    "JOB": "Jobs API",
    "PII": "PII Anonymization",
    "SELL": "Seller Entitlement Service",
}

# Curated per-threat risk + mitigation status. This is the ONLY place status is
# recorded, so the summary tables in README/risk-matrix/executive-summary and
# the JSON export can never disagree.
#
#   status: Mitigated | Partially Mitigated | Open | Accepted
#     Open     = a real gap with no effective control today (needs work)
#     Accepted = accurate finding, deliberately not mitigated, justified in the doc
STATUS: dict[str, tuple[int, str]] = {
    # Pipeline mode
    "PM.T01": (9, "Mitigated"),
    "PM.T02": (4, "Mitigated"),
    "PM.T03": (6, "Mitigated"),
    "PM.T04": (3, "Mitigated"),
    "PM.T05": (4, "Mitigated"),
    "PM.T06": (8, "Mitigated"),
    "PM.T07": (2, "Mitigated"),
    "PM.T08": (4, "Partially Mitigated"),
    # BDA mode
    "BDA.T01": (4, "Partially Mitigated"),
    "BDA.T02": (2, "Mitigated"),
    "BDA.T03": (3, "Mitigated"),
    "BDA.T04": (4, "Mitigated"),
    "BDA.T05": (2, "Mitigated"),
    # Agents
    "AGT.T01": (6, "Mitigated"),
    "AGT.T02": (4, "Mitigated"),
    "AGT.T03": (4, "Mitigated"),
    "AGT.T04": (2, "Mitigated"),
    "AGT.T05": (6, "Mitigated"),
    # Chat
    "CHAT.T01": (9, "Mitigated"),
    "CHAT.T02": (3, "Mitigated"),
    "CHAT.T03": (6, "Open"),
    "CHAT.T04": (3, "Mitigated"),
    "CHAT.T05": (4, "Partially Mitigated"),
    "CHAT.T06": (3, "Open"),
    # MCP
    "MCP.T01": (8, "Partially Mitigated"),
    "MCP.T02": (3, "Mitigated"),
    "MCP.T03": (6, "Partially Mitigated"),
    "MCP.T04": (6, "Mitigated"),
    "MCP.T05": (4, "Mitigated"),
    "MCP.T06": (2, "Mitigated"),
    # Knowledge base
    "KB.T01": (6, "Mitigated"),
    "KB.T02": (6, "Partially Mitigated"),
    "KB.T03": (2, "Mitigated"),
    "KB.T04": (2, "Mitigated"),
    # Auth / RBAC
    "AUTH.T01": (4, "Mitigated"),
    "AUTH.T02": (6, "Mitigated"),
    "AUTH.T03": (6, "Mitigated"),
    "AUTH.T04": (3, "Mitigated"),
    "AUTH.T05": (3, "Mitigated"),
    "AUTH.T06": (2, "Mitigated"),
    # Two consumers fail closed (the pii-anonymizer feature API and
    # chat_with_document_processor). Five NAMED scope-aware resolvers — six source
    # files, list_documents_* being two — still read a failed lookup as
    # "unrestricted"; the scope is keyed on an email that can diverge from the row;
    # what identifier the claims yield is unconstrained at the adapter; and
    # Chat-with-Document is unrestricted on the streaming transport, which forwards
    # no verified caller (GAP-07). All four are written out in the entry's Residual
    # risk field.
    "AUTH.T07": (6, "Partially Mitigated"),
    "AUTH.T08": (6, "Mitigated"),
    "AUTH.T09": (6, "Mitigated"),
    "AUTH.T10": (3, "Accepted"),
    "AUTH.T11": (3, "Mitigated"),
    "AUTH.T12": (3, "Mitigated"),
    "AUTH.T13": (4, "Partially Mitigated"),
    # Likelihood Low / Severity Medium, scored as its two nearest siblings
    # (AUTH.T10, AUTH.T11) are. "Partially Mitigated" because the entry's own
    # Mitigations field closes the identity-precedence and input-shape halves but
    # leaves the group check unenforceable on the streaming transport (GAP-07).
    "AUTH.T14": (3, "Partially Mitigated"),
    "AUTH.T15": (4, "Partially Mitigated"),
    "AUTH.T16": (6, "Partially Mitigated"),
    # SDK / CLI
    "SDK.T01": (6, "Partially Mitigated"),
    "SDK.T02": (6, "Partially Mitigated"),
    "SDK.T03": (3, "Mitigated"),
    "SDK.T04": (4, "Mitigated"),
    "SDK.T05": (8, "Open"),
    # Hooks
    "HOOK.T01": (4, "Partially Mitigated"),
    "HOOK.T02": (8, "Partially Mitigated"),
    "HOOK.T03": (3, "Mitigated"),
    "HOOK.T04": (4, "Mitigated"),
    "HOOK.T05": (3, "Mitigated"),
    "HOOK.T06": (6, "Mitigated"),
    "HOOK.T07": (6, "Open"),
    # Web UI
    "UI.T01": (6, "Partially Mitigated"),
    "UI.T02": (2, "Mitigated"),
    "UI.T03": (6, "Mitigated"),
    "UI.T04": (2, "Mitigated"),
    "UI.T05": (2, "Mitigated"),
    "UI.T06": (6, "Open"),
    "UI.T07": (3, "Mitigated"),
    # Reporting / analytics
    "RPT.T01": (2, "Mitigated"),
    "RPT.T02": (6, "Mitigated"),
    "RPT.T03": (2, "Mitigated"),
    "RPT.T04": (3, "Mitigated"),
    "RPT.T05": (6, "Mitigated"),
    "RPT.T06": (4, "Mitigated"),
    "RPT.T07": (6, "Partially Mitigated"),
    "RPT.T08": (3, "Partially Mitigated"),
    # Feature platform
    "FEAT.T01": (8, "Partially Mitigated"),
    "FEAT.T02": (2, "Accepted"),
    "FEAT.T03": (6, "Partially Mitigated"),
    "FEAT.T04": (3, "Partially Mitigated"),
    # Jobs API
    "JOB.T01": (6, "Mitigated"),
    "JOB.T02": (3, "Open"),
    "JOB.T03": (3, "Partially Mitigated"),
    # PII anonymization
    "PII.T01": (4, "Accepted"),
    "PII.T02": (3, "Partially Mitigated"),
    "PII.T03": (6, "Mitigated"),
    "PII.T04": (6, "Mitigated"),
    "PII.T05": (3, "Partially Mitigated"),
    # Seller Entitlement Service (seller-account assets, not customer)
    "SELL.T01": (6, "Mitigated"),
    "SELL.T02": (9, "Mitigated"),
    "SELL.T03": (6, "Mitigated"),
    "SELL.T04": (4, "Partially Mitigated"),
    "SELL.T05": (8, "Mitigated"),
    "SELL.T06": (4, "Partially Mitigated"),
    "SELL.T07": (2, "Mitigated"),
    "SELL.T08": (6, "Partially Mitigated"),
    "SELL.T09": (4, "Mitigated"),
    "SELL.T10": (4, "Accepted"),
}

BLOCK_RE = re.compile(
    r"^### (?P<id>[A-Z]+\.T\d+):\s*(?P<title>.+?)\s*$\n+(?P<table>(?:\|.*\n)+)",
    re.M,
)
ROW_RE = re.compile(
    r"^\|\s*\*\*(?P<key>[^*|]+?)\*\*\s*\|\s*(?P<val>.*?)\s*\|\s*$", re.M
)


#: Document Information rows in ``README.md`` that become export metadata, mapped
#: to their JSON key. These were hardcoded here until v3.2, and drifted: the
#: export claimed v0.6.3/3.0 while the README said v0.6.5.dev1/3.1. Reading them
#: from the README makes the README the single source and the drift impossible.
METADATA_ROWS = {
    "Version": "version",
    "Last Updated": "lastUpdated",
    "Applies to release": "appliesToRelease",
    "Last reviewed against version": "lastReviewedAgainstVersion",
}


def read_metadata() -> dict[str, str]:
    """Pull the Document Information rows out of ``README.md``.

    ``lastReviewedAgainstVersion`` is the field ``scripts/check_threat_model_currency.py``
    gates on; emitting it here means a consumer of the JSON export sees the same
    currency claim a reader of the README does.
    """
    rows = dict(ROW_RE.findall(INDEX.read_text()))
    values = {k.strip(): v.strip() for k, v in rows.items()}
    out: dict[str, str] = {}
    for label, key in METADATA_ROWS.items():
        if label not in values:
            raise SystemExit(
                f"README.md Document Information table has no '**{label}**' row; "
                f"the export's {key} is read from it"
            )
        out[key] = values[label]
    return out


def source_docs() -> list[Path]:
    return sorted(
        p for p in ROOT.rglob("*.md") if not any(m in str(p) for m in SKIP_MARKERS)
    )


def parse_threats() -> list[dict[str, object]]:
    threats: list[dict[str, object]] = []
    seen: set[str] = set()
    for doc in source_docs():
        rel = doc.relative_to(ROOT).as_posix()
        for m in BLOCK_RE.finditer(doc.read_text()):
            tid = m.group("id")
            rows: list[tuple[str, str]] = ROW_RE.findall(m.group("table"))
            fields = {k.strip(): v.strip() for k, v in rows}
            if "Threat ID" not in fields:
                continue  # a "### X.Tnn" heading that isn't a threat block
            if tid in seen:
                raise SystemExit(f"duplicate threat id {tid} (second in {rel})")
            seen.add(tid)
            score, status = STATUS.get(tid, (0, "UNKNOWN"))
            threats.append(
                {
                    "id": tid,
                    "title": m.group("title"),
                    "stride": fields.get("Category", "").replace("STRIDE: ", ""),
                    "description": fields.get("Description", ""),
                    "attackVector": fields.get("Attack Vector", ""),
                    "impact": fields.get("Impact", ""),
                    "likelihood": fields.get("Likelihood", ""),
                    "severity": fields.get("Severity", ""),
                    "riskScore": score,
                    "component": PREFIX_DOCS.get(tid.split(".")[0], ""),
                    "affectedComponents": fields.get("Affected Components", ""),
                    "status": status,
                    "mitigations": fields.get("Mitigations", ""),
                    "residualRisk": fields.get("Residual risk / recommendation")
                    or fields.get("Residual risk", ""),
                    "source": rel,
                }
            )
    threats.sort(
        key=lambda t: (str(t["id"]).split(".")[0], int(str(t["id"]).split(".T")[1]))
    )
    return threats


def band(score: int) -> str:
    return (
        "Critical"
        if score >= 8
        else "High"
        if score >= 6
        else "Medium"
        if score >= 3
        else "Low"
    )


def build() -> dict[str, object]:
    threats = parse_threats()
    ids = {str(t["id"]) for t in threats}
    missing = ids - set(STATUS)
    extra = set(STATUS) - ids
    if missing or extra:
        raise SystemExit(
            f"STATUS table drift — missing: {sorted(missing)} extra: {sorted(extra)}"
        )
    risk: dict[str, int] = {}
    status: dict[str, int] = {}
    for t in threats:
        b = band(int(t["riskScore"]))  # pyright: ignore[reportArgumentType]
        risk[b] = risk.get(b, 0) + 1
        st = str(t["status"])
        status[st] = status.get(st, 0) + 1
    return {
        "schema": "threat-composer/1.0",
        "projectName": "GenAI IDP Accelerator",
        "description": (
            "STRIDE threat model for the GenAI Intelligent Document Processing "
            "Accelerator (unified architecture: Pipeline + BDA modes). Generated "
            "from the Markdown corpus by scripts/build_threat_model.py — do not "
            "edit by hand."
        ),
        **read_metadata(),
        "threatCount": len(threats),
        "riskDistribution": risk,
        "mitigationStatus": status,
        "threats": threats,
    }


# --------------------------------------------------------------------------- #
# Prose counts vs the export
# --------------------------------------------------------------------------- #
# The corpus repeats its own tallies in prose and in summary tables across a dozen
# documents, and the export is the only place they are computed. Nothing compared
# the two, so they drifted by hand: the export carried 99 threats while four
# documents said 98, and the "Partially Mitigated" tally was a release behind in
# three of them. Every one of those was written by someone who had just read the
# generated numbers.
#
# So `--check` now reads the prose back. The files are listed explicitly rather
# than discovered, because adding a document that states a count should be a
# visible decision; `scripts/tests/` has no bearing on it either way. A listed
# file that does not exist is an ERROR, not a skip: a rename would otherwise
# narrow the gate silently, which is the same failure this whole check exists to
# stop.
_COUNTED_DOCS = (
    "README.md",
    "threat-id-glossary.md",
    "risk-assessment/risk-matrix.md",
    "deliverables/executive-summary.md",
    "deliverables/implementation-guide.md",
    "threat-analysis/stride-analysis.md",
    "../../docs/threat-model.md",
    # The repository's landing page. It stated a count nobody was checking, which
    # is the worst place in the tree for one.
    "../../README.md",
)

# ⚠️ WHAT THIS CHECK DOES NOT READ. It matches hand-fitted phrasings, so its reach
# is bounded and a reader should not mistake a pass for corpus-wide consistency:
#
#   * counts written as words ("ninety-nine"), or with a thousands separator;
#   * a count on a line that also records a DELTA — `_DELTA` strips the delta and
#     re-checks the rest, but a single line carrying both a delta and an unrelated
#     tally in a shape the delta-strip mangles can still slip;
#   * any table column this file does not name. Per-component counts and STRIDE
#     categories ARE read (both drifted once and are covered below); anything else
#     tabular is not;
#   * a document not in `_COUNTED_DOCS`.
#
# Three of the documents are held by a single bespoke pattern fitted to one
# sentence, so an ordinary rephrase drops that document's total with no signal.
# If you rewrite a sentence that states a count, check that a pattern here still
# matches it — deliberately verify by breaking the number and re-running.

# Every way the corpus writes the total. Each pattern must capture the number in
# group 1, and must be specific enough that an unrelated figure cannot match it.
_TOTAL_PATTERNS = (
    r"\|\s*\*\*Total Threats\*\*\s*\|\s*(\d+)\s*\|",
    r"\|\s*\*\*Total Threats Identified\*\*\s*\|\s*(\d+)\s*\|",
    r"\|\s*\*\*Total Threat IDs\*\*\s*\|\s*(\d+)\s*\|",
    r"\|\s*\*\*Total threats identified\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|",
    r"\|\s*Threats identified\s*\|\s*\*\*(\d+)\*\*\s*\|",
    r"pie title Risk Distribution \((\d+) Threats\)",
    r"All (\d+) threat IDs",
    r"\*\*(\d+) threats\*\* across",
    r"mitigate the (\d+) identified threats",
    r"holding (\d+) identifiers in their head",
    r"sum to more than (\d+) because",
    r"STRIDE model of (\d+) threats",
)

# Status and risk-band tallies, in the row shapes the corpus uses:
#   | **Mitigated** | 62 | ... |
#   | Mitigated | 62 (63%) |
#   | **Open** (real gap, needs work) | **6** | **6%** |
#
# The label must fill its whole cell, bar an optional bold wrapper and an optional
# parenthetical gloss. A looser `[^|]*` tail matched the register row "| KB.T03 |
# OpenSearch Serverless Data Exposure | **2** |" as a claim that there are 2
# `Open` threats — a status label is a prefix of ordinary prose, so the anchor at
# the closing pipe is what makes this readable at all.
_TALLY_ROW = r"\|\s*\*{{0,2}}{label}\*{{0,2}}(?:\s*\([^)|]*\))?\s*\|\s*\*{{0,2}}(\d+)"

# A DELTA is history, not a current count — the revision table is full of
# "93 → 98 threats" and "Mitigated 63 → 62". The delta EXPRESSION is stripped and
# the rest of the line still checked, rather than disabling the whole line: a
# whole-line skip means one `→` anywhere silently exempts every other count on it.
_DELTA = re.compile(r"\d+\s*(?:→|->)\s*\d+")

# A STRIDE cell may qualify the category ("Elevation of Privilege (detection
# gap)"); the tallies count the category.
_STRIDE_QUALIFIER = re.compile(r"\s*\([^)]*\)\s*$")

# Per-component rows, in the two shapes the corpus uses. Both are anchored at the
# start of the line because the component name is ordinary prose elsewhere.
#   | Authentication/RBAC | 0 | 6 | 9 | 1 | 16 |      (bands then total)
#   | AUTH | Authentication/RBAC | 16 | High (6) | ... |   (total only)
_COMPONENT_BANDS_ROW = (
    r"^\|\s*{component}\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*"
    r"\|\s*\*{{0,2}}(\d+)"
)
_COMPONENT_TOTAL_ROW = r"^\|\s*[A-Z]+\s*\|\s*{component}\s*\|\s*\*{{0,2}}(\d+)"

# The column-total row under a per-component table.
_TOTALS_ROW = (
    r"^\|\s*\*{0,2}Total\*{0,2}\s*\|\s*\*{0,2}(\d+)\*{0,2}\s*\|\s*\*{0,2}(\d+)\*{0,2}"
    r"\s*\|\s*\*{0,2}(\d+)\*{0,2}\s*\|\s*\*{0,2}(\d+)\*{0,2}\s*\|\s*\*{0,2}(\d+)"
)

# Band values inside a mermaid pie block: `"Medium (3-5)" : 44`.
_PIE_SLICE = r"\"{band} \([0-9–\-]+\)\"\s*:\s*(\d+)"

_BAND_ORDER = ("Critical", "High", "Medium", "Low")


def _iter_counted_docs():
    """Every document that states a count. Raises if one has moved."""
    missing = [rel for rel in _COUNTED_DOCS if not (ROOT / rel).resolve().is_file()]
    if missing:
        raise SystemExit(
            "_COUNTED_DOCS names file(s) that do not exist: "
            f"{missing} — fix the path rather than letting the count check "
            "quietly cover one document fewer"
        )
    return [(ROOT / rel).resolve() for rel in _COUNTED_DOCS]


def _component_tallies(doc: dict[str, object]) -> dict[str, dict[str, int]]:
    """Per-component band counts and totals, from the export."""
    out: dict[str, dict[str, int]] = {}
    for t in doc["threats"]:  # pyright: ignore[reportGeneralTypeIssues]
        comp = str(t["component"])
        entry = out.setdefault(comp, {b: 0 for b in _BAND_ORDER} | {"total": 0})
        entry[band(int(t["riskScore"]))] += 1
        entry["total"] += 1
    return out


def _stride_tallies(doc: dict[str, object]) -> dict[str, int]:
    """Per-STRIDE-category counts, from the export.

    A threat may carry several categories, so these sum to more than the total —
    which is exactly what the corpus's own note beside these tables says.
    """
    out: dict[str, int] = {}
    for t in doc["threats"]:  # pyright: ignore[reportGeneralTypeIssues]
        for part in str(t["stride"]).replace("STRIDE:", "").split(","):
            name = _STRIDE_QUALIFIER.sub("", part.strip())
            if name:
                out[name] = out.get(name, 0) + 1
    return out


def check_prose_counts(doc: dict[str, object]) -> list[str]:
    """Every count the corpus states in prose, compared with the export.

    Returns a list of human-readable mismatches; empty means consistent. See the
    bounded-reach note above `_COUNTED_DOCS` for what this does NOT read.
    """
    total = int(doc["threatCount"])  # pyright: ignore[reportArgumentType]
    risk: dict[str, int] = doc["riskDistribution"]  # pyright: ignore[reportAssignmentType]
    status: dict[str, int] = doc["mitigationStatus"]  # pyright: ignore[reportAssignmentType]
    components = _component_tallies(doc)
    stride = _stride_tallies(doc)
    band_labels = {
        "Critical": (r"Critical risk \(8–9\)", r"Critical risk \(score 8-9\)"),
        "High": (r"High risk \(6–7\)", r"High risk \(score 6-7\)"),
        "Medium": (r"Medium risk \(3–5\)", r"Medium risk \(score 3-5\)"),
        "Low": (r"Low risk \(1–2\)", r"Low risk \(score 1-2\)"),
    }
    problems: list[str] = []

    def report(rel, lineno: int, stated: str, what: str, expected: int) -> None:
        message = f"{rel}:{lineno}: states {stated} {what}, export has {expected}"
        if int(stated) != expected and message not in problems:
            problems.append(message)

    repo = ROOT.parent.parent
    for path in _iter_counted_docs():
        rel = path.relative_to(repo)
        raw = path.read_text().splitlines()
        # Each line, plus each line joined with the next. The second form catches a
        # count reflowed across a line break, which is how the root README's total
        # escaped a purely per-line check ("...a STRIDE model of 98\n  threats
        # across...").
        windows: list[tuple[int, str]] = []
        for lineno, line in enumerate(raw, 1):
            windows.append((lineno, line))
            if lineno < len(raw):
                windows.append((lineno, f"{line} {raw[lineno].strip()}"))

        for lineno, window in windows:
            window = _DELTA.sub("", window)
            for pattern in _TOTAL_PATTERNS:
                for found in re.finditer(pattern, window):
                    report(rel, lineno, found.group(1), "threats", total)
            for label, expected in status.items():
                for found in re.finditer(
                    _TALLY_ROW.format(label=re.escape(label)), window, re.IGNORECASE
                ):
                    report(rel, lineno, found.group(1), repr(label), expected)
            for band_name, labels in band_labels.items():
                for label in labels:
                    for found in re.finditer(
                        _TALLY_ROW.format(label=label), window, re.IGNORECASE
                    ):
                        report(
                            rel, lineno, found.group(1),
                            f"{band_name} threats", risk.get(band_name, 0),
                        )
            # STRIDE rows require the bold cell: the glossary carries a bare
            # "| Spoofing |" per-threat column that a looser anchor reads as a
            # tally.
            for name, expected in stride.items():
                for found in re.finditer(
                    rf"\|\s*\*\*{re.escape(name)}\*\*\s*\|\s*\*{{0,2}}(\d+)", window
                ):
                    report(rel, lineno, found.group(1), f"{name} threats", expected)
            for band_name in _BAND_ORDER:
                for found in re.finditer(
                    _PIE_SLICE.format(band=band_name), window
                ):
                    report(
                        rel, lineno, found.group(1),
                        f"{band_name} threats", risk.get(band_name, 0),
                    )
            for comp, tallies in components.items():
                escaped = re.escape(comp)
                for found in re.finditer(
                    _COMPONENT_BANDS_ROW.format(component=escaped), window
                ):
                    for i, band_name in enumerate(_BAND_ORDER):
                        report(
                            rel, lineno, found.group(i + 1),
                            f"{comp} {band_name} threats", tallies[band_name],
                        )
                    report(
                        rel, lineno, found.group(5),
                        f"{comp} threats", tallies["total"],
                    )
                for found in re.finditer(
                    _COMPONENT_TOTAL_ROW.format(component=escaped), window
                ):
                    report(
                        rel, lineno, found.group(1),
                        f"{comp} threats", tallies["total"],
                    )
            for found in re.finditer(_TOTALS_ROW, window):
                for i, band_name in enumerate(_BAND_ORDER):
                    report(
                        rel, lineno, found.group(i + 1),
                        f"{band_name} threats (column total)",
                        risk.get(band_name, 0),
                    )
                report(rel, lineno, found.group(5), "threats (column total)", total)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--check", action="store_true", help="verify committed JSON is current"
    )
    args = ap.parse_args()
    doc = build()
    rendered = json.dumps(doc, indent=2) + "\n"
    if args.check:
        current = OUT.read_text() if OUT.exists() else ""
        if current != rendered:
            print(
                f"{OUT.relative_to(ROOT.parent)} is out of date — "
                "run scripts/build_threat_model.py",
                file=sys.stderr,
            )
            return 1
        prose = check_prose_counts(doc)
        if prose:
            print(
                "prose counts disagree with the generated export — fix the "
                "documents, not the export:",
                file=sys.stderr,
            )
            for problem in prose:
                print(f"  {problem}", file=sys.stderr)
            return 1
        print(f"threat model export is current ({doc['threatCount']} threats)")
        print(
            f"  prose counts consistent across "
            f"{len(list(_iter_counted_docs()))} document(s)"
        )
        return 0
    OUT.write_text(rendered)
    print(f"wrote {OUT.relative_to(ROOT.parent)}: {doc['threatCount']} threats")
    print(f"  risk:   {doc['riskDistribution']}")
    print(f"  status: {doc['mitigationStatus']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
