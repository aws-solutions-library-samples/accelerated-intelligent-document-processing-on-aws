# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A setup step that FAILS must stop the expensive step behind it.

`run_matrix.py` used to print ``config <version>: FAIL`` and launch the grid
anyway. The result is worse than a crash: the cell runs against whatever
configuration the stack already holds — and ``Config#bench-*`` names are
deterministic and reused across grids, so a previous grid's version of the same
name is often still there — producing a complete set of plausible numbers
attributed to a configuration that never landed. Nothing in ``runmap.json`` said
so, and those numbers get written down.

The same shape existed in ``register_testset``, which discarded the ``aws s3 cp``
result entirely (not even bound to a name) and then wrote a metadata row
asserting ``status: READY`` and ``fileCount: 1``.
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("boto3")

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(BENCH, "harness")
sys.path.insert(0, HARNESS)

import run_matrix  # noqa: E402


class _Proc:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _cell(cell, version):
    return {"cell": cell, "version": version, "path": f"/tmp/{version}.yaml"}


# --------------------------------------------------------------------------
# upload_all_configs: the failure must be RETURNED, not just printed
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_upload_all_configs_returns_the_failed_versions(monkeypatch):
    calls = []

    def fake_upload(stack, version, path, res=None, native=False):
        calls.append(version)
        return version != "bench-b"

    monkeypatch.setattr(run_matrix, "upload_config", fake_upload)
    failed = run_matrix.upload_all_configs(
        "stk", [_cell("c1", "bench-a"), _cell("c2", "bench-b"), _cell("c3", "bench-c")]
    )
    assert failed == ["bench-b"]
    # Deduped by version, and a failure does not abort the remaining uploads:
    # a sibling arm whose config lands is still worth running.
    assert calls == ["bench-a", "bench-b", "bench-c"]


@pytest.mark.unit
def test_upload_all_configs_dedupes_versions(monkeypatch):
    calls = []
    monkeypatch.setattr(
        run_matrix,
        "upload_config",
        lambda s, v, p, res=None, native=False: calls.append(v) or True,
    )
    run_matrix.upload_all_configs(
        "stk", [_cell("c1", "bench-a"), _cell("c2", "bench-a")]
    )
    assert calls == ["bench-a"]


@pytest.mark.unit
def test_upload_all_configs_reports_every_failure_not_just_the_first(monkeypatch):
    monkeypatch.setattr(
        run_matrix, "upload_config", lambda s, v, p, res=None, native=False: False
    )
    failed = run_matrix.upload_all_configs(
        "stk", [_cell("c1", "bench-a"), _cell("c2", "bench-b")]
    )
    assert sorted(failed) == ["bench-a", "bench-b"]


@pytest.mark.unit
def test_failed_versions_select_exactly_the_cells_to_skip(monkeypatch):
    """The skip is per version, so a sibling cell on a good version survives.

    This is the filter main() applies to `pairs`/`ref_pairs`; asserting it here
    pins the semantics (skip the arm, not the matrix) independently of main's
    stack plumbing.
    """
    monkeypatch.setattr(
        run_matrix,
        "upload_config",
        lambda s, v, p, res=None, native=False: v != "bench-b",
    )
    cells = [_cell("c1", "bench-a"), _cell("c2", "bench-b"), _cell("c3", "bench-c")]
    failed = set(run_matrix.upload_all_configs("stk", cells))

    pairs = [(c, "doc1", 0) for c in cells]
    kept = [p for p in pairs if p[0]["version"] not in failed]
    assert [p[0]["cell"] for p in kept] == ["c1", "c3"]
    assert sorted({c["cell"] for c in cells if c["version"] in failed}) == ["c2"]


# --------------------------------------------------------------------------
# register_testset: no READY metadata row for a document that did not upload
# --------------------------------------------------------------------------


class _SpyDDB:
    def __init__(self):
        self.puts = []

    def put_item(self, **kw):
        self.puts.append(kw)


@pytest.mark.unit
def test_register_testset_writes_no_metadata_row_when_the_copy_fails(monkeypatch):
    spy = _SpyDDB()
    monkeypatch.setattr(run_matrix.lib, "ddb", lambda: spy)
    monkeypatch.setattr(
        run_matrix,
        "sh",
        lambda cmd: _Proc(returncode=1, stderr="fatal error: Access Denied"),
    )
    ok = run_matrix.register_testset(
        "stk",
        {"testset_bucket": "b", "tracking_table": "t"},
        "tiny_form",
        "/tmp/tiny_form.pdf",
    )
    assert ok is False
    assert spy.puts == [], (
        "a metadata row claiming status=READY / fileCount=1 was written for a "
        "document whose S3 copy failed"
    )


@pytest.mark.unit
def test_register_testset_writes_the_row_on_success(monkeypatch):
    spy = _SpyDDB()
    monkeypatch.setattr(run_matrix.lib, "ddb", lambda: spy)
    monkeypatch.setattr(run_matrix, "sh", lambda cmd: _Proc(returncode=0))
    ok = run_matrix.register_testset(
        "stk",
        {"testset_bucket": "b", "tracking_table": "t"},
        "tiny_form",
        "/tmp/tiny_form.pdf",
    )
    assert ok is True
    assert len(spy.puts) == 1
    assert spy.puts[0]["Item"]["status"]["S"] == "READY"


# --------------------------------------------------------------------------
# The failure is in the artifact, not only on stderr
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_runmap_records_the_skipped_cells():
    """main() writes these three keys; aggregate._meta must carry them into the
    committed summary, because the runmap itself is gitignored."""
    import inspect

    src = inspect.getsource(run_matrix.main)
    for key in (
        "config_upload_failed_versions",
        "cells_skipped_config_upload",
        "docs_missing_truth",
    ):
        assert f'"{key}"' in src, f"{key} is not written to runmap.json"

    import aggregate

    meta_src = inspect.getsource(aggregate._meta)
    for key in ("config_upload_failed_versions", "cells_skipped_config_upload"):
        assert f'rm.get("{key}")' in meta_src, (
            f"{key} is in runmap.json but never reaches summary.json's meta, "
            "which is the only durable record"
        )
