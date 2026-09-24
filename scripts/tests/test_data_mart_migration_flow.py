# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data-mart migration state machine — flow-shape invariants.

The full-flow branch and the resume branch both have to converge on
``WriteInProgressMarker`` before ``PlanChunks``. That guarantees the
SSM marker's ``started_at`` is refreshed on every entry to the chunk
phase, which is what keeps ``_migration_in_progress`` — the
scheduled hourly / daily / reconciler gate — closed during a resume
run more than ``_MIGRATION_IN_PROGRESS_STALE_SECONDS`` (2 h) after
the original start.

Without the resume-path convergence, a resume after 2 h opens the
gate, and the scheduled crons race the migration chunks against the
same trailing-24 h partitions with different Athena
``ClientRequestToken`` values, which is the exact race the gate
exists to close. Pin the shape here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASL_PATH = _REPO_ROOT / "src/statemachine/data_mart_migration.asl.json"

# Same substitution shape ``test_state_machine_provisioning_retry.py``
# uses — turn CFN ``${Placeholder}`` into a parseable literal.
_UNQUOTED_PLACEHOLDER_RE = re.compile(r"(:\s*)\$\{[^}]*\}(\s*[,\n\}\]])")
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")


def _load_asl() -> dict:
    text = _ASL_PATH.read_text()
    text = _UNQUOTED_PLACEHOLDER_RE.sub(r"\g<1>1\g<2>", text)
    text = _PLACEHOLDER_RE.sub(r"\g<1>", text)
    return json.loads(text)


@pytest.fixture(scope="module")
def asl() -> dict:
    return _load_asl()


@pytest.mark.unit
class TestResumeAnchorFlow:
    """The reviewer's Finding 1 (🔴): a resume path that bypasses
    WriteInProgressMarker leaves ``started_at`` stale, which — after
    the 2 h age bound was added to ``_migration_in_progress`` — opens
    the gate on the scheduled hourly / daily / reconciler crons and
    re-opens the duplicate-write race that gate exists to close."""

    def test_adopt_marker_anchor_routes_to_write_in_progress_marker(self, asl: dict):
        states = asl["States"]
        assert "AdoptMarkerAnchor" in states, (
            "AdoptMarkerAnchor state missing — the resume path relies on it "
            "to overwrite $.anchor with the marker-persisted value."
        )
        assert states["AdoptMarkerAnchor"]["Next"] == "WriteInProgressMarker", (
            "AdoptMarkerAnchor must route to WriteInProgressMarker so the "
            "resume path refreshes started_at on the SSM marker. Without "
            "this, a resume after _MIGRATION_IN_PROGRESS_STALE_SECONDS (2 h) "
            "opens the migration-in-progress gate on the scheduled crons "
            "and re-opens the duplicate-write race the gate exists to "
            "close. See docs/data-mart-migration-runbook.md."
        )

    def test_route_on_resume_anchor_guards_null_marker_anchor(self, asl: dict):
        states = asl["States"]
        assert "RouteOnResumeAnchor" in states, (
            "RouteOnResumeAnchor Choice missing — this state guards the "
            "null-marker-anchor case (marker predates anchor persistence) "
            "so AdoptMarkerAnchor is not entered with a null $.anchor "
            "that would then be persisted back to the marker."
        )
        route = states["RouteOnResumeAnchor"]
        assert route["Type"] == "Choice"
        # Null → skip AdoptMarkerAnchor, go straight to WriteInProgressMarker
        # so the caller's dispatcher-stamped $.anchor is preserved.
        null_route = next(
            (
                c
                for c in route["Choices"]
                if c.get("Variable") == "$.marker_check.anchor"
                and c.get("IsNull") is True
            ),
            None,
        )
        assert null_route is not None, (
            "RouteOnResumeAnchor must have an IsNull=true choice on "
            "$.marker_check.anchor so a null marker anchor bypasses "
            "AdoptMarkerAnchor's overwrite."
        )
        assert null_route["Next"] == "WriteInProgressMarker", (
            "Null-marker-anchor sub-branch must route to WriteInProgressMarker "
            "to preserve the caller's anchor AND refresh started_at."
        )
        assert route["Default"] == "AdoptMarkerAnchor", (
            "Non-null marker anchor must fall through to AdoptMarkerAnchor "
            "so the original run's window is restored."
        )

    def test_route_on_marker_state_skip_purge_reaches_route_on_resume_anchor(
        self, asl: dict
    ):
        states = asl["States"]
        route = states["RouteOnMarkerState"]
        assert route["Type"] == "Choice"
        skip_purge = next(
            (
                c
                for c in route["Choices"]
                if c.get("Variable") == "$.marker_check.should_skip_purge"
                and c.get("BooleanEquals") is True
            ),
            None,
        )
        assert skip_purge is not None, (
            "RouteOnMarkerState must retain the should_skip_purge branch — "
            "it is the resume entry point."
        )
        assert skip_purge["Next"] == "RouteOnResumeAnchor", (
            "The should_skip_purge branch must route through "
            "RouteOnResumeAnchor so the null-marker-anchor guard applies. "
            "Routing directly to AdoptMarkerAnchor would reintroduce the "
            "null-overwrite defect."
        )

    def test_write_in_progress_marker_leads_to_plan_chunks(self, asl: dict):
        # Both convergent branches (full-flow via InitialPurge, and
        # resume via AdoptMarkerAnchor / RouteOnResumeAnchor) enter
        # WriteInProgressMarker, which must then go on to PlanChunks.
        # A refactor that reordered these would silently un-serialise
        # the started_at refresh from the chunk phase.
        states = asl["States"]
        assert states["WriteInProgressMarker"]["Next"] == "PlanChunks", (
            "WriteInProgressMarker must lead directly to PlanChunks."
        )

    def test_initial_purge_still_upstream_of_write_in_progress_marker(self, asl: dict):
        # The full-flow path skips InitialPurge on resume ONLY because
        # RouteOnMarkerState → RouteOnResumeAnchor never routes
        # through it. If someone moved InitialPurge to be reached
        # from the resume path, the resume would destructively re-purge
        # partitions the first run had already written.
        states = asl["States"]
        assert states["InitialPurge"]["Next"] == "WriteInProgressMarker", (
            "InitialPurge must be immediately upstream of WriteInProgressMarker."
        )
