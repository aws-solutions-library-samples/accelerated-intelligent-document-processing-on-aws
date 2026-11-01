# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which test runs drafted a test set's labels (#1746, #1757).

The test-set resolver and the test-execution aggregation both answer this through
``idp_common.testset_label_jobs``; their own suites test it through each caller.
"""

import pytest

from idp_common.testset_label_jobs import (
    drafting_run_ids,
    label_job_run_id,
    legacy_drafting_run,
    oldest_first,
)


def _job(run_id, created="2026-01-01T00:00:00Z", **extra):
    return {"SK": f"labeljob#{run_id}", "jobId": run_id, "createdAt": created, **extra}


@pytest.mark.unit
class TestLabelJobRunId:
    def test_the_job_id(self):
        assert label_job_run_id({"jobId": "run1", "SK": "labeljob#other"}) == "run1"

    def test_the_key_when_there_is_no_job_id(self):
        assert label_job_run_id({"SK": "labeljob#run1"}) == "run1"

    @pytest.mark.parametrize("job", [{}, {"SK": "labeljob#"}, {"jobId": ""}])
    def test_none_when_nothing_names_a_run(self, job):
        assert label_job_run_id(job) is None


@pytest.mark.unit
class TestOldestFirst:
    def test_orders_by_creation_and_puts_undated_jobs_first_in_order(self):
        jobs = [
            _job("c", "2026-01-03T00:00:00Z"),
            {"jobId": "u1"},
            _job("a", "2026-01-01T00:00:00Z"),
            {"jobId": "u2", "createdAt": None},
        ]
        assert [j["jobId"] for j in oldest_first(jobs)] == ["u1", "u2", "a", "c"]


@pytest.mark.unit
class TestLegacyDraftingRun:
    def test_none_without_jobs(self):
        assert legacy_drafting_run([]) is None

    def test_none_when_the_oldest_job_names_no_earlier_run(self):
        assert legacy_drafting_run([_job("a")]) is None

    def test_the_earlier_run_when_it_has_no_record(self):
        assert legacy_drafting_run([_job("a", previousLabelJobId="old")]) == "old"

    def test_none_when_the_earlier_run_is_a_recorded_job(self):
        jobs = [_job("a", previousLabelJobId="b"), _job("b", "2026-01-02T00:00:00Z")]
        assert legacy_drafting_run(jobs) is None

    def test_only_the_oldest_job_is_read(self):
        jobs = [_job("a"), _job("b", "2026-01-02T00:00:00Z", previousLabelJobId="x")]
        assert legacy_drafting_run(jobs) is None


@pytest.mark.unit
class TestDraftingRunIds:
    def test_the_pointer_alone_without_jobs(self):
        assert drafting_run_ids([], "runL") == ["runL"]

    @pytest.mark.parametrize("pointer", [None, ""])
    def test_nothing_without_jobs_or_pointer(self, pointer):
        assert drafting_run_ids([], pointer) == []

    def test_the_pointer_is_not_read_once_a_job_exists(self):
        assert drafting_run_ids([_job("a")], "runP") == ["a"]

    def test_every_job_oldest_first_after_the_legacy_run(self):
        jobs = [
            _job("a", previousLabelJobId="old"),
            _job("b", "2026-01-02T00:00:00Z", status="COMPLETED"),
            _job("c", "2026-01-03T00:00:00Z", status="RUNNING"),
        ]
        assert drafting_run_ids(jobs, "c") == ["old", "a", "b", "c"]

    def test_a_failed_job_is_left_out(self):
        jobs = [_job("a"), _job("f", "2026-01-02T00:00:00Z", status="FAILED")]
        assert drafting_run_ids(jobs, "f") == ["a"]

    def test_a_failed_oldest_job_still_names_the_run_before_it(self):
        jobs = [_job("f", status="FAILED", previousLabelJobId="old")]
        assert drafting_run_ids(jobs, "f") == ["old"]

    def test_only_failed_jobs_drafted_nothing(self):
        jobs = [_job("f", status="FAILED")]
        assert drafting_run_ids(jobs, "f") == []

    def test_a_job_naming_no_run_is_dropped(self):
        assert drafting_run_ids([{"status": "COMPLETED"}, _job("a")], None) == ["a"]
