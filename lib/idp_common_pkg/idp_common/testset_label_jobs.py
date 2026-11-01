# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which test runs drafted a test set's labels.

A test set's draft labels come from labeling jobs, one item per job under the
set's partition (SK ``labeljob#<runId>``); the job id is a test run id. Starting a
job repoints the set's ``labelJobId`` and keeps the run the pointer named before
as the job's ``previousLabelJobId``.

Lives in ``idp_common`` because two deployables need the same answer: the
test-set resolver (a set's configuration profile, its confidence fingerprint) and
the test-execution aggregation (whether a scoring run measures a configuration
against labels it drafted itself). The answers drifted when each read the job list
its own way (#1746, #1757).

Pure functions over the job items: reading them is the caller's business.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional

#: The job status a labeling job ends with when it labeled nothing.
FAILED = "FAILED"


def oldest_first(jobs: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """``jobs`` ordered by ``createdAt``, oldest first; undated ones lead, stably."""
    return sorted(jobs, key=lambda job: str(job.get("createdAt") or ""))


def label_job_run_id(job: Mapping[str, Any]) -> Optional[str]:
    """The test run a labeling-job item records: its ``jobId``, else its SK."""
    return job.get("jobId") or str(job.get("SK", "")).split("#", 1)[-1] or None


def legacy_drafting_run(jobs: List[Mapping[str, Any]]) -> Optional[str]:
    """The run the set's pointer named before its oldest job, when that run has
    no job record of its own; otherwise None.

    Every job is recorded before the pointer moves, so on the oldest job that run
    is normally a recorded job too. When it is not, it is named nowhere else once
    the pointer has moved, and it is counted as a drafting run. ``jobs`` must be
    oldest first (:func:`oldest_first`).
    """
    if not jobs:
        return None
    previous = jobs[0].get("previousLabelJobId")
    if not previous:
        return None
    if previous in {label_job_run_id(job) for job in jobs}:
        return None
    return previous


def drafting_run_ids(
    jobs: List[Mapping[str, Any]], pointer: Optional[str]
) -> List[str]:
    """Every test run that drafted labels for a set, oldest first.

    Each labeling job's run, preceded by the pointer's unrecorded earlier run when
    there is one (:func:`legacy_drafting_run`); a set with no job record at all is
    named only by its ``labelJobId`` ``pointer``. A FAILED job is left out: a job
    ends FAILED only when it labeled nothing, so it drafted none of the set's
    labels, and counting its run would let a re-extract that failed under another
    configuration make the set's profile read as mixed. ``jobs`` must be oldest
    first (:func:`oldest_first`).
    """
    if not jobs:
        return [pointer] if pointer else []
    run_ids = [label_job_run_id(job) for job in jobs if job.get("status") != FAILED]
    legacy = legacy_drafting_run(jobs)
    if legacy:
        run_ids.insert(0, legacy)
    return [run_id for run_id in run_ids if run_id]
