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
if the corpus and the STATUS table below have drifted apart — wire this into CI
to keep them locked together.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deliverables" / "threat-model.tc.json"

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
    "AUTH.T07": (6, "Mitigated"),
    "AUTH.T08": (6, "Mitigated"),
    "AUTH.T09": (6, "Mitigated"),
    "AUTH.T10": (3, "Accepted"),
    "AUTH.T11": (3, "Mitigated"),
    "AUTH.T12": (3, "Mitigated"),
    # SDK / CLI
    "SDK.T01": (6, "Partially Mitigated"),
    "SDK.T02": (6, "Partially Mitigated"),
    "SDK.T03": (3, "Mitigated"),
    "SDK.T04": (4, "Mitigated"),
    # Hooks
    "HOOK.T01": (4, "Partially Mitigated"),
    "HOOK.T02": (8, "Partially Mitigated"),
    "HOOK.T03": (3, "Mitigated"),
    "HOOK.T04": (4, "Mitigated"),
    "HOOK.T05": (3, "Mitigated"),
    "HOOK.T06": (6, "Mitigated"),
    # Web UI
    "UI.T01": (6, "Partially Mitigated"),
    "UI.T02": (2, "Mitigated"),
    "UI.T03": (6, "Mitigated"),
    "UI.T04": (2, "Mitigated"),
    "UI.T05": (2, "Mitigated"),
    "UI.T06": (6, "Open"),
    "UI.T07": (3, "Open"),
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
        "appliesToRelease": "v0.6.3",
        "lastUpdated": "2026-07-28",
        "version": "3.0",
        "threatCount": len(threats),
        "riskDistribution": risk,
        "mitigationStatus": status,
        "threats": threats,
    }


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
        print(f"threat model export is current ({doc['threatCount']} threats)")
        return 0
    OUT.write_text(rendered)
    print(f"wrote {OUT.relative_to(ROOT.parent)}: {doc['threatCount']} threats")
    print(f"  risk:   {doc['riskDistribution']}")
    print(f"  status: {doc['mitigationStatus']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
