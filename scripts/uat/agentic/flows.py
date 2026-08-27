#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Testable claims.

DESIGN RULE 3: the flow list is DETERMINISTIC. In the full system these are derived
from the CHANGELOG section under test (one flow per claim, stable slug from the heading)
so the denominator cannot move between releases. For the prototype they are hand-written
against real published claims, with the same shape the generator will emit.

Each flow carries:
  claim             what the product PROMISES, in the user's language. This is all a
                    COLD worker ever sees.
  docs              the documented procedure. WARM only.
  documented_steps  how many steps the docs imply — the denominator for the
                    "complexity gap" metric (documented vs actual).
  verify            how to confirm the claim OUT OF BAND (never by asking the agent).
"""

from __future__ import annotations

import re
from pathlib import Path

FLOW_DIR = Path(__file__).resolve().parent / "flows"
REPO_ROOT = Path(__file__).resolve().parents[3]

FLOWS: dict[str, dict] = {
    # ---------------------------------------------------------------------------
    # Read-only. Deliberately first: it mutates nothing, needs no seeded state, and
    # still has a hard external oracle (the configured class list in DynamoDB). It
    # tests a real question — can a user discover what this deployment actually
    # extracts? — which is the "is the promise redeemable" failure mode in miniature.
    # ---------------------------------------------------------------------------
    "config-discovery": {
        "flow_id": "config-discovery",
        "claim": (
            "This deployment is configured to extract structured data from a specific "
            "set of document types. As a user, find out which document types (classes) "
            "this deployment is set up to process, and report their names."
        ),
        "docs": (
            "From the left navigation, open **Configuration > View/Edit Configuration**. "
            "The configuration editor opens on the active configuration version. The "
            "document classes it defines are listed there; each class has a name and a "
            "set of fields that are extracted from it."
        ),
        "documented_steps": 2,
        "verify": {
            "method": "ddb_config_classes",
            # The agent must name at least this fraction of the real classes for the
            # claim to count as confirmed. Not 100%: a user reporting 5 of 6 classes has
            # still demonstrably found the feature.
            "min_recall": 0.6,
        },
    },
    # ---------------------------------------------------------------------------
    # Read-only. Tests whether the *processing outcome* is legible — the thing a user
    # most needs and the area accelerator_issues.md concentrates in.
    # ---------------------------------------------------------------------------
    "document-outcome-legibility": {
        "flow_id": "document-outcome-legibility",
        "claim": (
            "After a document is processed, you can see whether it succeeded and, if it "
            "did not, find out why. Determine the processing outcome of the documents in "
            "this deployment, and for any that did not succeed, report the reason given."
        ),
        "docs": (
            "Open **Document List** from the left navigation. Each row shows the "
            "document's **Status** and, where relevant, a **Processing Issues** control "
            "that explains problems encountered during processing."
        ),
        "documented_steps": 2,
        "verify": {
            "method": "ddb_document_status",
            "min_recall": 0.5,
        },
    },
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "ad-hoc"


def _read_docs_ref(ref: str) -> str | None:
    """Load documented prose from `path/to/doc.md#anchor`, VERBATIM.

    Verbatim matters: a finding filed against a paraphrase of the docs is a finding about
    the paraphrase. Returns None when the file or anchor cannot be resolved, so a WARM run
    degrades to cold rather than silently testing invented prose.
    """
    path_part, _, anchor = ref.partition("#")
    path = REPO_ROOT / path_part
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if not anchor:
        return text[:6000]
    # Match the heading whose slug equals the anchor, take until the next heading of the
    # same or higher level.
    for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.M):
        if _slug(m.group(2)) != anchor.lower():
            continue
        level = len(m.group(1))
        rest = text[m.end() :]
        nxt = re.search(rf"^#{{1,{level}}}\s+", rest, re.M)
        return (m.group(0) + "\n" + (rest[: nxt.start()] if nxt else rest))[:6000]
    return None


def load_yaml_flows() -> dict[str, dict]:
    """Flow files in ./flows/*.yaml. Human-editable, reviewable, no Python required."""
    out: dict[str, dict] = {}
    if not FLOW_DIR.is_dir():
        return out
    try:
        import yaml
    except ImportError:
        return out
    for f in sorted(FLOW_DIR.glob("*.y*ml")):
        try:
            spec = yaml.safe_load(f.read_text()) or {}
        except Exception:
            continue
        fid = spec.get("flow_id") or f.stem
        spec["flow_id"] = fid
        if spec.get("docs_ref") and not spec.get("docs"):
            resolved = _read_docs_ref(spec["docs_ref"])
            if resolved is None:
                # Surface it: a renamed heading must read as lost WARM coverage, not as a
                # silent pass against prose that was never in the docs.
                print(
                    f"[flows] WARNING {fid}: docs_ref {spec['docs_ref']!r} did not "
                    "resolve — WARM runs for this flow will degrade to cold."
                )
            spec["docs"] = resolved
        spec.setdefault("verify", {"method": "none"})
        out[fid] = spec
    return out


def ad_hoc(goal: str) -> dict:
    """A one-sentence target with no oracle.

    Deliberately unverifiable: `method: none` yields `confirmed: None`, never success.
    You get the friction report; you do not get a verdict, because nothing checked that
    the task actually happened. Promote it to a flow file to make it verifiable.
    """
    return {
        "flow_id": f"adhoc-{_slug(goal)}",
        "claim": goal,
        "docs": None,
        "documented_steps": None,
        "verify": {"method": "none"},
        "ad_hoc": True,
    }


def all_flows() -> dict[str, dict]:
    """Builtins plus flow files. A flow file wins on id collision, so a YAML file can
    override a builtin without editing Python."""
    merged = dict(FLOWS)
    merged.update(load_yaml_flows())
    return merged
