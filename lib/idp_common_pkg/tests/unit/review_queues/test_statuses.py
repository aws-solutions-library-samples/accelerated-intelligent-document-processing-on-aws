# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import ast
import subprocess  # nosec B404 - fixed argv, no shell
import sys
from pathlib import Path

import pytest

from idp_common.review_queues import statuses
from idp_common.review_queues.statuses import (
    LEGACY_DONE,
    LEGACY_PENDING,
    LIVE_ITEM_STATES,
    TERMINAL_ITEM_STATES,
    HitlView,
    LegacyHitlStatus,
    QueueItemState,
    ReviewStatus,
    normalize_hitl_status,
    review_status_for_state,
)

STATUSES_PATH = Path(statuses.__file__)


@pytest.mark.unit
class TestEnums:
    def test_review_status_values(self):
        assert [s.value for s in ReviewStatus] == ["in_review", "complete", "rejected"]

    def test_queue_item_states(self):
        assert {s.value for s in QueueItemState} == {
            "HOLDING",
            "OPEN",
            "CLAIMED",
            "FINALIZING",
            "COMPLETE",
            "REJECTED",
            "FINALIZE_FAILED",
            "WITHDRAWN",
        }

    def test_legacy_spellings(self):
        assert {s.value for s in LegacyHitlStatus} == {
            "PendingReview",
            "Review Pending",
            "InProgress",
            "ReviewInProgress",
            "Completed",
            "Review Completed",
            "Skipped",
            "Review Skipped",
        }

    def test_legacy_sets_partition_the_pending_and_done_spellings(self):
        assert LEGACY_PENDING.isdisjoint(LEGACY_DONE)
        assert LegacyHitlStatus.REVIEW_IN_PROGRESS not in LEGACY_PENDING | LEGACY_DONE

    def test_item_state_sets_partition_the_enum(self):
        assert LIVE_ITEM_STATES | TERMINAL_ITEM_STATES == set(QueueItemState)
        assert LIVE_ITEM_STATES.isdisjoint(TERMINAL_ITEM_STATES)


@pytest.mark.unit
class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "kind"),
        [
            ("PendingReview", "pending"),
            ("Review Pending", "pending"),
            ("InProgress", "in_progress"),
            ("ReviewInProgress", "in_progress"),
            ("Completed", "complete"),
            ("Review Completed", "complete"),
            ("Skipped", "skipped"),
            ("Review Skipped", "skipped"),
        ],
    )
    def test_legacy_spellings_are_not_queue_managed(self, raw, kind):
        assert normalize_hitl_status(raw) == HitlView(kind, False)

    @pytest.mark.parametrize(
        ("raw", "kind"),
        [
            ("in_review", "in_review"),
            ("complete", "complete"),
            ("rejected", "rejected"),
        ],
    )
    def test_review_values_are_queue_managed(self, raw, kind):
        assert normalize_hitl_status(raw) == HitlView(kind, True)

    @pytest.mark.parametrize("raw", [None, "", "garbage", "pendingreview"])
    def test_unknown_reads_as_none(self, raw):
        assert normalize_hitl_status(raw) == HitlView("none", False)

    def test_strip(self):
        assert normalize_hitl_status("  in_review ") == HitlView("in_review", True)


@pytest.mark.unit
class TestReviewStatusForState:
    @pytest.mark.parametrize("state", list(QueueItemState))
    def test_total_over_enum_and_string(self, state):
        assert review_status_for_state(state) == review_status_for_state(state.value)

    def test_mapping(self):
        assert review_status_for_state("OPEN") is ReviewStatus.IN_REVIEW
        assert review_status_for_state("FINALIZE_FAILED") is ReviewStatus.IN_REVIEW
        assert review_status_for_state("COMPLETE") is ReviewStatus.COMPLETE
        assert review_status_for_state("REJECTED") is ReviewStatus.REJECTED
        assert review_status_for_state("WITHDRAWN") is None


@pytest.mark.unit
class TestStandardLibraryOnly:
    def test_imports_only_stdlib(self):
        tree = ast.parse(STATUSES_PATH.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        assert imported <= {"dataclasses", "enum", "typing", "__future__"}

    def test_package_import_does_not_import_boto3(self):
        out = subprocess.run(  # nosec B603 - fixed argv
            [
                sys.executable,
                "-c",
                "import sys, idp_common.review_queues as r; r.ReviewStatus; "
                "print('boto3' in sys.modules, 'pydantic' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout.strip() == "False False"
