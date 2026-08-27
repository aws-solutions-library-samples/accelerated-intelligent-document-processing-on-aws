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
