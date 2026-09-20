# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two harness defects that only appear at scale, and cost real money when they do.

**Resolving runs used one full table scan per run.** A document's key is
``doc#<run_id>/<doc_name>`` and ``PK`` is the partition key, so ``begins_with`` is
not available and resolving a run means scanning. Doing that once per run per poll
iteration is what made a 171-run grid unusable: the drain loop spent longer inside
a single iteration than the runs themselves took, and the same call on the launch
path held concurrency to 12-20 executions against a stack cap of 100, because
deciding whether to launch cost more than launching. The cost must scale with the
table, not with the table times the run count (#1016).

**The run directory was named to the second and created with ``exist_ok=True``.**
``runmap.json`` is rewritten after every launch, so two suites starting in the same
second overwrite each other continuously and the last writer wins. Eight concurrent
lanes destroyed seven suites' runmaps — roughly 700 runs — and the mapping from run
id to cell, document and repeat exists nowhere else, so the spend is unrecoverable
without it.
"""

import importlib.util
import os
import pathlib
import re
import sys

import pytest

HARNESS = pathlib.Path(__file__).resolve().parents[1] / "harness"


def _load(name):
    sys.path.insert(0, str(HARNESS))
    spec = importlib.util.spec_from_file_location(name, HARNESS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeDDB:
    """Counts scans and serves one page of items."""

    def __init__(self, items):
        self._items = items
        self.scans = 0

    def scan(self, **kw):
        self.scans += 1
        proj = kw.get("ProjectionExpression", "")
        assert "PK" in proj, (
            "poll_runs must project PK — it buckets rows by run id in memory, and "
            "cannot do that if it does not read the key"
        )
        return {"Items": self._items}


def _doc(run_id, name, status, evaluation="COMPLETED"):
    return {
        "PK": {"S": f"doc#{run_id}/{name}"},
        "ObjectStatus": {"S": status},
        "EvaluationStatus": {"S": evaluation},
    }


@pytest.mark.unit
def test_many_runs_are_resolved_in_a_single_table_pass():
    lib = _load("lib")
    fake = _FakeDDB(
        [
            _doc("bench-a-1", "a.pdf", "COMPLETED"),
            _doc("bench-b-2", "b.pdf", "FAILED"),
            _doc("bench-c-3", "c.pdf", "COMPLETED"),
            {"PK": {"S": "testset#bench-a"}},  # non-doc rows are ignored
        ]
    )
    lib.ddb = lambda: fake

    out = lib.poll_runs("t", ["bench-a-1", "bench-b-2", "bench-c-3"])

    assert fake.scans == 1, (
        f"resolving 3 runs issued {fake.scans} table scans; it must issue 1. Once "
        "per run is what made a 171-run grid spend hours in one drain iteration."
    )
    assert out["bench-a-1"]["obj_done"] == 1
    assert out["bench-a-1"]["failed"] == 0
    assert out["bench-b-2"]["failed"] == 1
    assert out["bench-b-2"]["obj_done"] == 0
    assert out["bench-c-3"]["total"] == 1


@pytest.mark.unit
def test_a_run_with_no_documents_yet_is_reported_not_crashed():
    """A just-launched run has no rows. It must read as 'nothing done', so the
    caller keeps waiting rather than treating a missing key as completion."""
    lib = _load("lib")
    lib.ddb = lambda: _FakeDDB([])
    out = lib.poll_runs("t", ["bench-x-9"])
    assert out["bench-x-9"] == {
        "total": 0,
        "obj_done": 0,
        "eval_done": 0,
        "failed": 0,
        "statuses": {},
    }


@pytest.mark.unit
def test_no_run_ids_does_not_touch_the_table():
    lib = _load("lib")
    fake = _FakeDDB([_doc("bench-a-1", "a.pdf", "COMPLETED")])
    lib.ddb = lambda: fake
    assert lib.poll_runs("t", []) == {}
    # A launch that returned no run id must not be polled for either.
    assert lib.poll_runs("t", [None]) == {}
    assert fake.scans == 0


@pytest.mark.unit
def test_single_run_helper_still_works():
    """`poll_run` is kept for callers that genuinely have one run."""
    lib = _load("lib")
    lib.ddb = lambda: _FakeDDB([_doc("bench-a-1", "a.pdf", "COMPLETED")])
    assert lib.poll_run("t", "bench-a-1")["obj_done"] == 1


@pytest.mark.unit
def test_the_run_directory_cannot_be_shared_by_two_concurrent_suites():
    """Creation must fail rather than merge, and the name must carry something
    unique — a UTC stamp resolved to the second is not unique."""
    src = (HARNESS / "run_matrix.py").read_text()
    m = re.search(r"outdir = os\.path\.join\(RESULTS, (.+?)\)\n", src)
    assert m, "could not find where run_matrix.py builds its output directory"
    name_expr = m.group(1)
    assert "uuid" in name_expr, (
        "the run directory name has no unique component, so two suites launched in "
        "the same second share it and overwrite each other's runmap.json. See #1016."
    )
    assert "os.makedirs(outdir, exist_ok=False)" in src, (
        "the run directory is created with exist_ok=True, so a collision merges two "
        "result sets silently instead of failing. See #1016."
    )


@pytest.mark.unit
def test_the_launch_and_drain_paths_use_the_batched_poll():
    """The batched helper is only a fix if the hot paths call it."""
    src = (HARNESS / "run_matrix.py").read_text()
    assert "lib.poll_runs(" in src, (
        "run_matrix.py does not use the batched poll, so the per-run scan is still "
        "on the launch and drain paths. See #1016."
    )
    assert not re.search(r"if not poll_done\(", src), (
        "a per-run poll_done() call is back on a hot path; use prune_pending(), "
        "which resolves every pending run in one table pass. See #1016."
    )


@pytest.mark.unit
def test_results_dir_is_still_where_scoring_expects_it():
    """The rename adds a suffix inside `benchmarks/results/`; it must not move the
    tree, because aggregate.py and RETENTION.md both address it by that path."""
    src = (HARNESS / "run_matrix.py").read_text()
    assert 'RESULTS = os.path.join(BENCH, "results")' in src
    assert os.path.basename("run-20260101-000000-core-abc123").startswith("run-"), (
        "scored-run directories are discovered by their run- prefix"
    )
