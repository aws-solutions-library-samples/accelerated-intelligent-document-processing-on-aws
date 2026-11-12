# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bulk Skip All Reviews async jobs: the worker step, on moto."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from idp_common.review_queues import bulk_jobs
from idp_common.review_queues import keys as rkeys
from idp_common.review_queues.bulk_jobs import job_view
from idp_common.review_queues.bulk_models import (
    DEFER_SECONDS,
    MAX_JOB_DOCS,
    BulkOutcome,
    BulkSkipRequest,
    DocumentOutcome,
    JobStatus,
)
from idp_common.review_queues.exceptions import ReviewValidationError

from ._bulk_env import ADMIN, REASON, REV, SCOPED, make_env

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 3, 31, tzinfo=timezone.utc)
PLENTY = lambda: 800.0  # noqa: E731 - seconds left in the invocation


@pytest.fixture
def env(review_table):
    return make_env(review_table)


def _job(env, caller, keys):
    for k in keys:
        env.doc(k, HITLStatus="Review Pending")
    result = env.service.submit(
        caller,
        BulkSkipRequest(mode="SELECTED", object_keys=list(keys), reason=REASON),
    )
    assert result.run_mode == "ASYNC"
    return result.job_id, env.sent[-1][1]


def _meta(env, job_id):
    return env.jobs.get(job_id)


@pytest.mark.unit
class TestWorkerStep:
    def test_a_selected_job_runs_to_completion_across_pages(self, env):
        keys = [f"d{i:02d}.pdf" for i in range(30)]
        job_id, wake = _job(env, REV, keys)
        assert env.service.run_job_step(job_id, wake, PLENTY) is None
        view = job_view(_meta(env, job_id))
        assert view.status is JobStatus.SUCCEEDED and view.processed == 30
        assert {c.outcome: c.count for c in view.counts}[BulkOutcome.SKIPPED] == 30
        assert all(env.status(k) == "Review Skipped" for k in keys)
        # The lock is released.
        assert env.jobs.acquire_lock("brj-" + "1" * 24, 0)

    def test_a_page_processed_twice_counts_once(self, env):
        job_id, wake = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        env.service.run_job_step(job_id, wake, PLENTY)
        env.jobs.update(
            job_id, {"Status": "RUNNING", "NextPage": 0}, removes=("FinishedAt",)
        )
        env.service.run_job_step(job_id, wake, PLENTY)
        view = job_view(_meta(env, job_id))
        assert view.processed == 7

    def test_a_result_is_recorded_and_counted_once(self, env):
        job_id, _wake = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        result = DocumentOutcome(object_key="d0.pdf", outcome=BulkOutcome.SKIPPED)
        assert env.jobs.record_result(job_id, result, 0) is True
        assert env.jobs.record_result(job_id, result, 0) is False
        assert job_view(_meta(env, job_id)).processed == 1

    def test_a_stale_wake_up_is_dropped(self, env):
        job_id, _wake = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        assert env.service.run_job_step(job_id, "not-the-token", PLENTY) is None
        assert job_view(_meta(env, job_id)).status is JobStatus.QUEUED

    def test_another_running_job_defers_this_one(self, env):
        job_id, wake = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        assert env.jobs.acquire_lock("brj-" + "2" * 24, 10**15)
        assert env.service.run_job_step(job_id, wake, PLENTY) == DEFER_SECONDS
        assert job_view(_meta(env, job_id)).status is JobStatus.QUEUED

    def test_a_deep_document_queue_defers_the_step(self, review_table):
        env = make_env(review_table, queue_depth=lambda: 1000)
        job_id, wake = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        assert env.service.run_job_step(job_id, wake, PLENTY) == DEFER_SECONDS
        assert job_view(_meta(env, job_id)).processed == 0

    def test_out_of_time_continues_from_the_next_page(self, env):
        job_id, wake = _job(env, REV, [f"d{i:02d}.pdf" for i in range(30)])
        left = iter([800.0, 10.0])
        assert env.service.run_job_step(job_id, wake, lambda: next(left)) == 0
        assert job_view(_meta(env, job_id)).processed == 25
        env.service.schedule(job_id, 0)
        assert env.service.run_job_step(job_id, env.sent[-1][1], PLENTY) is None
        assert job_view(_meta(env, job_id)).processed == 30

    def test_a_revoked_scope_fails_the_job(self, review_table):
        env = make_env(review_table, scope_resolver=lambda meta: ["other"])
        env.doc("p.pdf", HITLStatus="Review Pending")
        request = BulkSkipRequest(
            mode="PROFILE",
            profile="claims",
            submitted_from=T0,
            submitted_to=T1,
            reason=REASON,
        )
        result = env.service.create_job(SCOPED, request, "brj-" + "b" * 24, 5)
        assert env.service.run_job_step(result.job_id, env.sent[-1][1], PLENTY) is None
        view = job_view(_meta(env, result.job_id))
        assert (view.status, view.error) == (JobStatus.FAILED, "scope_revoked")

    def test_fail_job_releases_the_lock_and_notifies(self, review_table):
        notifier = MagicMock()
        env = make_env(review_table, notifier=notifier)
        job_id, _ = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        env.jobs.acquire_lock(job_id, 0)
        env.service.fail_job(job_id, "dead_lettered")
        view = job_view(_meta(env, job_id))
        assert (view.status, view.error) == (JobStatus.FAILED, "dead_lettered")
        notifier.assert_called_once()
        assert env.jobs.acquire_lock("brj-" + "3" * 24, 0)

    def test_stalled_is_computed_from_the_heartbeat(self, env):
        job_id, _ = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        meta = _meta(env, job_id)
        assert job_view(meta).stalled is False
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        assert job_view(meta, later).stalled is True

    def test_results_page_and_filter(self, env):
        keys = [f"d{i}.pdf" for i in range(7)]
        job_id, wake = _job(env, REV, keys)
        env.doc("d0.pdf", HITLStatus="Review Completed")
        env.service.run_job_step(job_id, wake, PLENTY)
        page = env.service.get_job(REV, job_id, outcome="ALREADY_FINISHED")
        assert [r.object_key for r in page.results] == ["d0.pdf"]
        first = env.service.get_job(REV, job_id, limit=3)
        assert len(first.results) == 3 and first.next_token

    def test_lists_are_per_requester_and_all_for_admins(self, env):
        _job(env, REV, [f"r{i}.pdf" for i in range(7)])
        _job(env, SCOPED, [f"s{i}.pdf" for i in range(7)])
        assert len(env.service.list_jobs(REV).jobs) == 1
        assert len(env.service.list_jobs(ADMIN).jobs) == 2


@pytest.mark.unit
class TestRequesterSub:
    def test_the_job_records_the_requester_sub_when_there_is_one(self, env):
        with_sub = REV.model_copy(update={"sub": "sub-1"})
        job_id, _ = _job(env, with_sub, [f"d{i}.pdf" for i in range(7)])
        assert _meta(env, job_id)["RequesterSub"] == "sub-1"
        other, _ = _job(env, SCOPED, [f"s{i}.pdf" for i in range(7)])
        assert "RequesterSub" not in _meta(env, other)


@pytest.mark.unit
class TestCancel:
    def test_a_queued_job_is_cancelled_and_never_starts(self, review_table):
        notifier = MagicMock()
        env = make_env(review_table, notifier=notifier)
        keys = [f"d{i}.pdf" for i in range(7)]
        job_id, wake = _job(env, REV, keys)
        view = env.service.cancel_job(REV, job_id)
        assert view.status is JobStatus.CANCELLED and view.finished_at
        notifier.assert_called_once()
        assert env.service.run_job_step(job_id, wake, PLENTY) is None
        assert job_view(_meta(env, job_id)).processed == 0
        assert all(env.status(k) == "Review Pending" for k in keys)

    def test_a_running_job_stops_before_its_next_page(self, env):
        keys = [f"d{i:02d}.pdf" for i in range(30)]
        job_id, wake = _job(env, REV, keys)
        left = iter([800.0, 10.0])
        assert env.service.run_job_step(job_id, wake, lambda: next(left)) == 0
        env.service.cancel_job(REV, job_id)
        env.service.schedule(job_id, 0)
        assert env.service.run_job_step(job_id, env.sent[-1][1], PLENTY) is None
        view = job_view(_meta(env, job_id))
        # The first page stays skipped; the second never runs.
        assert (view.status, view.processed) == (JobStatus.CANCELLED, 25)
        assert env.status("d29.pdf") == "Review Pending"
        assert env.jobs.acquire_lock("brj-" + "4" * 24, 0)

    def test_a_cancel_during_the_last_page_is_not_overwritten(self, env):
        job_id, wake = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        real = env.jobs.record_result

        def cancel_midway(*a, **kw):
            if job_view(_meta(env, job_id)).status is not JobStatus.CANCELLED:
                env.service.cancel_job(ADMIN, job_id)
            return real(*a, **kw)

        with patch.object(env.jobs, "record_result", side_effect=cancel_midway):
            env.service.run_job_step(job_id, wake, PLENTY)
        assert job_view(_meta(env, job_id)).status is JobStatus.CANCELLED

    def test_a_finished_job_cannot_be_cancelled(self, env):
        job_id, wake = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        env.service.run_job_step(job_id, wake, PLENTY)
        with pytest.raises(ReviewValidationError) as e:
            env.service.cancel_job(REV, job_id)
        assert e.value.code == "JOB_FINISHED"

    def test_only_the_requester_or_an_admin_can_cancel(self, env):
        job_id, _ = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        assert env.service.cancel_job(SCOPED, job_id) is None
        assert job_view(_meta(env, job_id)).status is JobStatus.QUEUED
        assert env.service.cancel_job(ADMIN, job_id).status is JobStatus.CANCELLED


@pytest.mark.unit
class TestDelete:
    def test_a_finished_job_is_deleted_from_every_list(self, env):
        job_id, wake = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        env.service.run_job_step(job_id, wake, PLENTY)
        assert env.service.delete_job(REV, job_id) is True
        assert env.service.get_job(REV, job_id) is None
        assert env.service.list_jobs(REV).jobs == []
        assert env.service.list_jobs(ADMIN).jobs == []
        # Its result rows remain only until they expire, unreachable without META.
        rows = env.jobs.client.query(
            "PK = :pk", None, {":pk": rkeys.bulk_job_pk(job_id)}, 100, None
        )["Items"]
        assert rows and all(r.get("ExpiresAfter") for r in rows)

    def test_a_cancelled_job_can_be_deleted(self, env):
        job_id, _ = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        env.service.cancel_job(REV, job_id)
        assert env.service.delete_job(REV, job_id) is True

    def test_an_active_job_is_refused(self, env):
        job_id, _ = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        with pytest.raises(ReviewValidationError) as e:
            env.service.delete_job(REV, job_id)
        assert e.value.code == "JOB_ACTIVE"
        assert env.service.get_job(REV, job_id) is not None

    def test_another_users_job_is_not_found(self, env):
        job_id, _ = _job(env, REV, [f"d{i}.pdf" for i in range(7)])
        env.service.cancel_job(REV, job_id)
        assert env.service.delete_job(SCOPED, job_id) is False
        assert env.service.get_job(REV, job_id) is not None


@pytest.mark.unit
class TestScale:
    def test_ten_thousand_documents_finish_with_paced_resubmits(self, review_table):
        env = make_env(review_table, max_concurrent_workflows=200)
        keys = [f"bulk/{i:05d}.pdf" for i in range(MAX_JOB_DOCS)]

        def fake_pages(profile, start, end, *, cursor=None, page_size=25):
            offset = int((cursor or {}).get("o", 0))
            chunk = keys[offset : offset + page_size]
            nxt = offset + page_size
            yield chunk, ({"o": nxt} if nxt < len(keys) else None)

        def fake_skip(deps, caller, key, **kw):
            return DocumentOutcome(
                object_key=key, outcome=BulkOutcome.SKIPPED, kind="legacy"
            )

        env.service.selection = MagicMock(pages=fake_pages)
        meta = env.service._new_meta(
            ADMIN,
            BulkSkipRequest(
                mode="PROFILE",
                profile="claims",
                submitted_from=T0,
                submitted_to=T1,
                reason=REASON,
            ),
            "brj-" + "a" * 24,
            kind=bulk_jobs.JobKind.ASYNC,
            status=JobStatus.QUEUED,
            total=0,
        )
        meta["WakeToken"] = "w0"
        env.jobs.create(meta)
        steps = 0
        wake = "w0"
        with patch.object(bulk_jobs, "skip_document", side_effect=fake_skip) as skip:
            while True:
                delay = env.service.run_job_step(meta["JobId"], wake, PLENTY)
                steps += 1
                if delay is None:
                    break
                per_step = skip.call_count
                assert per_step <= env.service.pacing_cap * steps + 25
                env.service.schedule(meta["JobId"], delay)
                wake = env.sent[-1][1]
                assert steps < 2000
        view = job_view(env.jobs.get(meta["JobId"]))
        assert view.status is JobStatus.SUCCEEDED
        assert view.processed == MAX_JOB_DOCS == view.total
        assert steps >= MAX_JOB_DOCS // (env.service.pacing_cap + 25)
