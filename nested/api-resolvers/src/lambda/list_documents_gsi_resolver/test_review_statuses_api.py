# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The vendored ``review_statuses`` copy answers like the canonical module.

The copy is byte-identical to ``idp_common/review_queues/statuses.py``
(``test_review_modules_vendored.py``); this exercises the predicates this resolver's
filters are built from, through the copy the resolver actually imports.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import review_statuses as rs  # noqa: E402


@pytest.mark.unit
class TestVendoredReviewStatuses:
    def test_finished_and_pending(self):
        assert rs.is_review_finished("Review Skipped") is True
        assert rs.is_review_finished("COMPLETED") is True
        assert rs.is_review_finished("in_review") is False
        assert set(rs.FINISHED_HITL_STATUSES) >= {"complete", "rejected", "COMPLETED"}
        assert rs.is_review_raised({"HITLStatus": "Review Pending"}) is True
        assert rs.is_review_raised({"HITLStatus": "N/A"}) is False
        assert rs.is_legacy_review_pending({"HITLTriggered": True}) is True
        assert rs.is_legacy_review_pending({"HITLStatus": "in_review"}) is False

    def test_pending_filter_and_constants(self):
        expression, names, values = rs.pending_filter_expression(":p")
        assert ":p" in expression and "#pf_st" in names and ":pf_true" in values
        assert "skipped" in rs.MACHINE_RESULT_DISPOSITIONS
        assert rs.QUEUE_MANAGED_MESSAGE.startswith("QUEUE_MANAGED:")
        assert "Spot check" in rs.SEND_TO_REVIEW_REASONS
        assert rs.REVIEWER_ACTIVE_STATUSES
        assert rs.review_status_for_state("OPEN") is rs.ReviewStatus.IN_REVIEW
