# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bulk Skip All Reviews: dry run and the inline (synchronous) path, on moto."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from idp_common.review_queues.bulk_models import (
    BulkOutcome,
    BulkSkipRequest,
    JobKind,
)

from ._bulk_env import ADMIN, REASON, REV, SCOPED, make_env

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 3, 31, tzinfo=timezone.utc)


@pytest.fixture
def env(review_table):
    return make_env(review_table)


def _selected(keys, **kw):
    return BulkSkipRequest(mode="SELECTED", object_keys=list(keys), reason=REASON, **kw)


def _profile(**kw):
    return BulkSkipRequest(
        mode="PROFILE",
        profile="claims",
        submitted_from=T0,
        submitted_to=T1,
        reason=REASON,
        **kw,
    )


def _mixed(env):
    env.doc("legacy.pdf", HITLStatus="Review Pending")
    env.doc("queue.pdf", HITLStatus="in_review")
    env.item("queue.pdf")
    env.doc("held.pdf", HITLStatus="in_review")
    env.item("held.pdf", state="CLAIMED", owner=ADMIN)
    env.doc("done.pdf", HITLStatus="Review Completed")
    env.doc("test.pdf", HITLStatus="Review Pending", TestSetId="ts")
    return ["legacy.pdf", "queue.pdf", "held.pdf", "done.pdf", "test.pdf", "nope.pdf"]


def _counts(result):
    return {c.outcome: c.count for c in result.counts if c.count}


@pytest.mark.unit
class TestDryRun:
    def test_dry_run_counts_equal_run_outcomes_and_write_nothing(self, env):
        keys = _mixed(env)
        dry = env.service.submit(REV, _selected(keys, dry_run=True))
        assert dry.dry_run and dry.run_mode == "ASYNC"  # 6 keys > limit 5
        assert env.status("legacy.pdf") == "Review Pending"
        assert env.store.get_item("queue.pdf").state == "OPEN"
        assert dry.eligible_count == 2
        # The run of the same keys (below the limit) agrees with the dry run.
        run = env.service.submit(REV, _selected(keys[:5]))
        assert _counts(run) == {
            BulkOutcome.SKIPPED: 2,
            BulkOutcome.HELD_BY_OTHER: 1,
            BulkOutcome.ALREADY_FINISHED: 1,
            BulkOutcome.TEST_STUDIO_EXCLUDED: 1,
        }
        expected = dict(_counts(dry))
        expected.pop(BulkOutcome.NOT_PERMITTED)
        assert _counts(run) == expected

    def test_profile_dry_run_is_exact_for_a_small_range(self, env):
        _mixed(env)
        dry = env.service.submit(REV, _profile(dry_run=True))
        assert dry.approximate is False and dry.eligible_count == 2
        assert dry.run_mode == "SYNC"

    def test_out_of_scope_profile_is_refused_before_any_read(self, env):
        env.doc("legacy.pdf", HITLStatus="Review Pending")
        scoped = SCOPED.model_copy(update={"allowed_versions": ["other"]})
        with patch.object(env.service.selection, "collect") as collect:
            with pytest.raises(PermissionError):
                env.service.submit(scoped, _profile(dry_run=True))
        collect.assert_not_called()


@pytest.mark.unit
class TestInline:
    def test_selected_mode_within_the_limit_runs_inline_and_is_recorded(self, env):
        env.doc("a.pdf", HITLStatus="Review Pending")
        result = env.service.submit(REV, _selected(["a.pdf"]))
        assert result.run_mode == "SYNC" and result.job_id
        assert result.results[0].outcome is BulkOutcome.SKIPPED
        assert env.status("a.pdf") == "Review Skipped"
        listed = env.service.list_jobs(REV)
        assert [j.job_id for j in listed.jobs] == [result.job_id]
        assert listed.jobs[0].kind is JobKind.SYNC
        page = env.service.get_job(REV, result.job_id)
        assert page.results[0].outcome is BulkOutcome.SKIPPED

    def test_admission_stops_at_the_budget(self, review_table):
        ticks = iter([0.0, 0.0] + [100.0] * 10)  # deadline, first doc, then late
        env = make_env(review_table, monotonic=lambda: next(ticks), max_workers=1)
        for k in "abc":
            env.doc(f"{k}.pdf", HITLStatus="Review Pending")
        result = env.service.submit(REV, _selected(["a.pdf", "b.pdf", "c.pdf"]))
        outcomes = [r.outcome for r in result.results]
        assert outcomes[0] is BulkOutcome.SKIPPED
        assert outcomes[1:] == [BulkOutcome.NOT_ATTEMPTED] * 2
        assert env.status("c.pdf") == "Review Pending"

    def test_same_client_request_id_returns_the_same_job(self, env):
        env.doc("a.pdf", HITLStatus="Review Pending")
        first = env.service.submit(REV, _selected(["a.pdf"], client_request_id="r-1"))
        again = env.service.submit(REV, _selected(["a.pdf"], client_request_id="r-1"))
        assert again.job_id == first.job_id
        assert again.results[0].outcome is BulkOutcome.SKIPPED

    def test_another_users_job_reads_as_not_found(self, env):
        env.doc("a.pdf", HITLStatus="Review Pending")
        result = env.service.submit(REV, _selected(["a.pdf"]))
        assert env.service.get_job(SCOPED, result.job_id) is None
        assert env.service.get_job(ADMIN, result.job_id) is not None
        assert env.service.list_jobs(SCOPED).jobs == []
        assert len(env.service.list_jobs(ADMIN).jobs) == 1

    def test_above_the_limit_creates_a_job(self, env):
        keys = [f"{i}.pdf" for i in range(7)]
        result = env.service.submit(REV, _selected(keys))
        assert result.run_mode == "ASYNC" and env.sent
        view = env.service.get_job(REV, result.job_id).job
        assert (view.status.value, view.total) == ("QUEUED", 7)
